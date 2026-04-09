from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from watchdog.events import FileSystemEvent, FileSystemEventHandler, FileMovedEvent
from watchdog.observers.polling import PollingObserver

LOGGER = logging.getLogger("file-watcher")


@dataclass(slots=True)
class Settings:
    cockpit_api_url: str
    watch_dirs: list[Path]
    archive_dir: Path | None
    state_path: Path
    source: str
    user_id: str
    chunking_strategy: str
    settle_seconds: float
    poll_interval_seconds: float
    max_file_bytes: int
    max_content_chars: int
    allowed_extensions: set[str]
    task_webhook_enabled: bool
    openrouter_api_key: str
    openrouter_free_models: list[str]
    openrouter_timeout_seconds: float


def _env(key: str, default: str) -> str:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip()


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _parse_dirs(raw: str) -> list[Path]:
    items = [part.strip() for part in raw.split(",")]
    parsed = [Path(item).expanduser().resolve() for item in items if item]
    return parsed


def _parse_extensions(raw: str) -> set[str]:
    parts = [part.strip().lower().lstrip(".") for part in raw.split(",") if part.strip()]
    if not parts:
        return {
            "txt",
            "md",
            "markdown",
            "csv",
            "json",
            "log",
            "yaml",
            "yml",
            "xml",
            "html",
            "htm",
            "py",
            "js",
            "ts",
            "tsx",
            "jsx",
            "sql",
            "sh",
            "env",
            "ini",
            "toml",
        }
    return set(parts)


def load_settings() -> Settings:
    watch_dirs = _parse_dirs(_env("FILE_WATCHER_WATCH_DIRS", "/watch/inbox"))
    if not watch_dirs:
        raise RuntimeError("FILE_WATCHER_WATCH_DIRS has no valid paths")

    archive_raw = _env("FILE_WATCHER_ARCHIVE_DIR", "/watch/processed")
    archive_dir = Path(archive_raw).expanduser().resolve() if archive_raw else None

    return Settings(
        cockpit_api_url=_env("FILE_WATCHER_COCKPIT_API_URL", "http://cockpit-api:8000").rstrip("/"),
        watch_dirs=watch_dirs,
        archive_dir=archive_dir,
        state_path=Path(_env("FILE_WATCHER_STATE_PATH", "/state/file_state.json")).expanduser().resolve(),
        source=_env("FILE_WATCHER_SOURCE", "file_watchdog"),
        user_id=_env("FILE_WATCHER_USER_ID", "watchdog"),
        chunking_strategy=_env("FILE_WATCHER_CHUNKING_STRATEGY", "semantic"),
        settle_seconds=max(_env_float("FILE_WATCHER_SETTLE_SECONDS", 2.0), 0.2),
        poll_interval_seconds=max(_env_float("FILE_WATCHER_POLL_INTERVAL_SECONDS", 1.0), 0.2),
        max_file_bytes=max(_env_int("FILE_WATCHER_MAX_FILE_BYTES", 2_500_000), 16_384),
        max_content_chars=max(_env_int("FILE_WATCHER_MAX_CONTENT_CHARS", 400_000), 10_000),
        allowed_extensions=_parse_extensions(_env("FILE_WATCHER_ALLOWED_EXTENSIONS", "txt,md,markdown,csv,json,log,yaml,yml,xml,html,htm,py,js,ts,tsx,jsx,sql,sh,env,ini,toml")),
        task_webhook_enabled=_env_bool("FILE_WATCHER_ENABLE_TASK_WEBHOOK", True),
        openrouter_api_key=_env("OPENROUTER_API_KEY", ""),
        openrouter_free_models=[
            item.strip() for item in _env("OPENROUTER_FREE_MODELS", "qwen/qwen3-32b:free").split(",") if item.strip()
        ],
        openrouter_timeout_seconds=max(_env_float("OPENROUTER_TIMEOUT_SECONDS", 45.0), 5.0),
    )


class StateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"files": {}}
        self._load()

    def _load(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text('{"files": {}}', encoding="utf-8")
            return

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            LOGGER.warning("State file invalid JSON, recreating at %s", self._path)
            self._path.write_text('{"files": {}}', encoding="utf-8")
            return

        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            LOGGER.warning("State file invalid structure, recreating at %s", self._path)
            self._path.write_text('{"files": {}}', encoding="utf-8")
            return

        self._state = data

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=True, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def is_already_processed(self, path: Path, fingerprint: str) -> bool:
        with self._lock:
            files = self._state.get("files", {})
            entry = files.get(str(path))
            if not isinstance(entry, dict):
                return False
            return str(entry.get("fingerprint", "")) == fingerprint

    def upsert(
        self,
        *,
        path: Path,
        fingerprint: str,
        size_bytes: int,
        rag_job_id: str | None,
        category: str,
        priority: str,
        task_count: int,
    ) -> None:
        with self._lock:
            files = self._state.setdefault("files", {})
            files[str(path)] = {
                "fingerprint": fingerprint,
                "size_bytes": size_bytes,
                "category": category,
                "priority": priority,
                "task_count": task_count,
                "rag_job_id": rag_job_id,
                "updated_at_epoch": int(time.time()),
            }
            self._save()


class QueueingHandler(FileSystemEventHandler):
    def __init__(self, q: queue.Queue[Path]) -> None:
        self._queue = q

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle_event(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle_event(event)

    def on_moved(self, event: FileMovedEvent) -> None:
        if event.is_directory:
            return
        try:
            self._queue.put(Path(event.dest_path).resolve(), block=False)
        except Exception:
            LOGGER.exception("Unable to enqueue moved file event: %s", event.dest_path)

    def _handle_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        try:
            self._queue.put(Path(event.src_path).resolve(), block=False)
        except Exception:
            LOGGER.exception("Unable to enqueue file event: %s", event.src_path)


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None

    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    fragment = text[start : end + 1]
    try:
        value = json.loads(fragment)
    except json.JSONDecodeError:
        return None

    if isinstance(value, dict):
        return value
    return None


def _file_fingerprint(blob: bytes) -> str:
    return hashlib.sha1(blob).hexdigest()


def _read_text_file(path: Path, max_bytes: int, max_chars: int) -> tuple[str, str, int] | None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None

    if size <= 0:
        return None

    if size > max_bytes:
        LOGGER.info("Skipping %s (size %d > max %d bytes)", path, size, max_bytes)
        return None

    blob = path.read_bytes()

    sample = blob[:1024]
    if b"\x00" in sample:
        LOGGER.info("Skipping %s (appears binary)", path)
        return None

    text: str | None = None
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            text = blob.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        LOGGER.info("Skipping %s (unable to decode text)", path)
        return None

    text = text.strip()
    if not text:
        return None

    if len(text) > max_chars:
        text = text[:max_chars]

    return text, _file_fingerprint(blob), size


def _heuristic_classification(text: str, filename: str) -> dict[str, Any]:
    lowered = f"{filename}\n{text[:8000]}".lower()

    category = "uncategorized"
    keyword_map = {
        "finance": ["fattura", "invoice", "iban", "bonifico", "pagamento", "costo", "spesa", "budget", "tax"],
        "health": ["medico", "ospedale", "referto", "esame", "sintomo", "terapia", "farmaco", "salute"],
        "legal": ["contratto", "clausola", "privacy", "gdpr", "termini", "compliance", "firma", "accordo"],
        "work": ["cliente", "progetto", "meeting", "roadmap", "delivery", "kpi", "sprint", "task"],
        "learning": ["tutorial", "guida", "lesson", "corso", "book", "articolo", "research"],
        "operations": ["deploy", "incident", "errore", "server", "backup", "monitoring", "log"],
        "personal": ["famiglia", "vacanza", "casa", "personale", "hobby", "amico"],
    }

    for candidate, keywords in keyword_map.items():
        if any(kw in lowered for kw in keywords):
            category = candidate
            break

    priority = "medium"
    high_priority_keywords = ["urgente", "urgent", "asap", "entro", "deadline", "scadenza", "immediato"]
    low_priority_keywords = ["quando puoi", "non urgente", "someday", "maybe"]
    if any(kw in lowered for kw in high_priority_keywords):
        priority = "high"
    elif any(kw in lowered for kw in low_priority_keywords):
        priority = "low"

    tasks: list[str] = []
    bullet_pattern = re.compile(r"^\s*[-*]\s+(.+)$")
    todo_pattern = re.compile(r"\b(?:todo|to do|da fare|devo|ricordami)\b", re.IGNORECASE)

    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        bullet_match = bullet_pattern.match(clean)
        if bullet_match:
            tasks.append(bullet_match.group(1).strip())
            if len(tasks) >= 8:
                break
            continue
        if todo_pattern.search(clean):
            tasks.append(clean)
            if len(tasks) >= 8:
                break

    summary = text[:220].replace("\n", " ").strip()

    return {
        "category": category,
        "priority": priority,
        "summary": summary,
        "tasks": tasks,
        "model": "heuristic",
    }


def _openrouter_classification(settings: Settings, text: str, filename: str) -> dict[str, Any] | None:
    if not settings.openrouter_api_key:
        return None

    system_prompt = (
        "You classify personal productivity files. "
        "Return strict JSON only with fields: "
        "category, priority, summary, tasks. "
        "category must be one of: finance, health, legal, work, personal, operations, learning, uncategorized. "
        "priority must be one of: low, medium, high. "
        "summary max 160 chars. "
        "tasks must be array of at most 6 actionable strings."
    )
    user_prompt = (
        f"Filename: {filename}\n"
        "Content excerpt:\n"
        f"{text[:8000]}"
    )

    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }

    models = [model for model in settings.openrouter_free_models if model.endswith(":free")]
    if not models:
        models = ["qwen/qwen3-32b:free"]

    with httpx.Client(timeout=settings.openrouter_timeout_seconds) as client:
        for model in models:
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
                "max_tokens": 350,
            }

            try:
                response = client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=body)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("OpenRouter request error on model %s: %s", model, exc)
                continue

            if response.status_code >= 400:
                LOGGER.warning("OpenRouter error %s on model %s", response.status_code, model)
                continue

            try:
                payload = response.json()
                content = str(payload["choices"][0]["message"]["content"])
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("OpenRouter invalid payload on model %s: %s", model, exc)
                continue

            parsed = _extract_json_object(content)
            if not parsed:
                continue

            category = str(parsed.get("category", "uncategorized")).strip().lower()
            priority = str(parsed.get("priority", "medium")).strip().lower()
            summary = str(parsed.get("summary", "")).strip()
            tasks_value = parsed.get("tasks")

            if category not in {"finance", "health", "legal", "work", "personal", "operations", "learning", "uncategorized"}:
                category = "uncategorized"
            if priority not in {"low", "medium", "high"}:
                priority = "medium"

            tasks: list[str] = []
            if isinstance(tasks_value, list):
                for value in tasks_value:
                    task = str(value).strip()
                    if task:
                        tasks.append(task)
                    if len(tasks) >= 6:
                        break

            return {
                "category": category,
                "priority": priority,
                "summary": summary[:160],
                "tasks": tasks,
                "model": model,
            }

    return None


def classify_file(settings: Settings, text: str, filename: str) -> dict[str, Any]:
    classified = _openrouter_classification(settings, text, filename)
    if classified:
        return classified
    return _heuristic_classification(text, filename)


def _post_json_with_retry(
    client: httpx.Client,
    *,
    url: str,
    payload: dict[str, Any],
    max_attempts: int = 4,
) -> dict[str, Any]:
    delay = 0.5
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001
            if attempt == max_attempts:
                raise RuntimeError(f"request_failed:{url}:{exc}") from exc
            time.sleep(delay)
            delay *= 2
            continue

        if response.status_code in {429, 500, 502, 503, 504}:
            if attempt == max_attempts:
                raise RuntimeError(f"request_http_{response.status_code}:{url}")
            time.sleep(delay)
            delay *= 2
            continue

        if response.status_code >= 400:
            raise RuntimeError(f"request_http_{response.status_code}:{url}:{response.text[:280]}")

        try:
            return response.json()
        except Exception:
            return {}

    raise RuntimeError("unexpected_retry_state")


def _choose_watch_root(path: Path, roots: list[Path]) -> Path | None:
    for root in roots:
        try:
            path.relative_to(root)
            return root
        except ValueError:
            continue
    return None


def _is_hidden(path: Path, watch_root: Path) -> bool:
    try:
        rel = path.relative_to(watch_root)
    except ValueError:
        return False
    return any(part.startswith(".") for part in rel.parts)


def _format_task_message(filename: str, category: str, priority: str, tasks: list[str], summary: str) -> str:
    lines = [
        f"Nuovo file analizzato: {filename}",
        f"Categoria: {category}",
        f"Priorita: {priority}",
        f"Sintesi: {summary[:200]}",
    ]
    if tasks:
        lines.append("Task estratti:")
        for idx, task in enumerate(tasks[:6], start=1):
            lines.append(f"{idx}. {task}")
    return "\n".join(lines)


def _archive_file(path: Path, watch_root: Path, archive_dir: Path | None) -> None:
    if archive_dir is None:
        return

    try:
        relative = path.relative_to(watch_root)
    except ValueError:
        return

    destination = archive_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(path, destination)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("Archive copy failed for %s -> %s: %s", path, destination, exc)


def process_file(path: Path, settings: Settings, state: StateStore, client: httpx.Client) -> None:
    if not path.exists() or not path.is_file():
        return

    watch_root = _choose_watch_root(path, settings.watch_dirs)
    if watch_root is None:
        return

    if _is_hidden(path, watch_root):
        return

    extension = path.suffix.lower().lstrip(".")
    if extension not in settings.allowed_extensions:
        LOGGER.info("Skipping %s (extension .%s not allowed)", path, extension)
        return

    time.sleep(settings.settle_seconds)

    read_result = _read_text_file(path, settings.max_file_bytes, settings.max_content_chars)
    if read_result is None:
        return

    content, fingerprint, size_bytes = read_result
    if state.is_already_processed(path, fingerprint):
        return

    classification = classify_file(settings, content, path.name)
    category = str(classification.get("category", "uncategorized"))
    priority = str(classification.get("priority", "medium"))
    summary = str(classification.get("summary", ""))
    tasks_raw = classification.get("tasks")
    tasks = tasks_raw if isinstance(tasks_raw, list) else []
    tasks = [str(task).strip() for task in tasks if str(task).strip()]

    document_id = hashlib.sha1(f"{str(path)}::{fingerprint}".encode("utf-8")).hexdigest()

    metadata = {
        "path": str(path),
        "filename": path.name,
        "size_bytes": size_bytes,
        "fingerprint": fingerprint,
        "category": category,
        "priority": priority,
        "summary": summary,
        "tasks": tasks,
        "classifier_model": str(classification.get("model", "heuristic")),
        "watch_root": str(watch_root),
    }

    rag_payload = {
        "document_id": document_id,
        "title": path.name,
        "source": settings.source,
        "content": content,
        "chunking_strategy": settings.chunking_strategy,
        "metadata": metadata,
    }

    rag_url = f"{settings.cockpit_api_url}/rag/documents/ingest"
    rag_response = _post_json_with_retry(client, url=rag_url, payload=rag_payload)
    rag_job_id = str(rag_response.get("job_id", "")).strip() or None

    LOGGER.info(
        "Ingested %s (category=%s priority=%s rag_job=%s)",
        path,
        category,
        priority,
        rag_job_id,
    )

    if settings.task_webhook_enabled and tasks:
        task_payload = {
            "source": "system",
            "user_id": settings.user_id,
            "message": _format_task_message(path.name, category, priority, tasks, summary),
            "metadata": {
                "origin": "file_watcher",
                "path": str(path),
                "document_id": document_id,
                "category": category,
                "priority": priority,
                "tasks": tasks,
                "rag_job_id": rag_job_id,
            },
        }
        inbox_url = f"{settings.cockpit_api_url}/webhooks/inbox"
        try:
            _post_json_with_retry(client, url=inbox_url, payload=task_payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Task webhook failed for %s: %s", path, exc)

    state.upsert(
        path=path,
        fingerprint=fingerprint,
        size_bytes=size_bytes,
        rag_job_id=rag_job_id,
        category=category,
        priority=priority,
        task_count=len(tasks),
    )

    _archive_file(path, watch_root, settings.archive_dir)


class Runner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = StateStore(settings.state_path)
        self.queue: queue.Queue[Path] = queue.Queue(maxsize=4096)
        self.shutdown_event = threading.Event()
        self.observer = PollingObserver(timeout=settings.poll_interval_seconds)

    def _ensure_dirs(self) -> None:
        for directory in self.settings.watch_dirs:
            directory.mkdir(parents=True, exist_ok=True)

        if self.settings.archive_dir is not None:
            self.settings.archive_dir.mkdir(parents=True, exist_ok=True)

    def _bootstrap_existing_files(self) -> None:
        for root in self.settings.watch_dirs:
            for path in root.rglob("*"):
                if path.is_file():
                    try:
                        self.queue.put(path.resolve(), block=False)
                    except queue.Full:
                        LOGGER.warning("Bootstrap queue full, skipping %s", path)

    def _install_signal_handlers(self) -> None:
        def _handle_signal(signum: int, _frame: Any) -> None:
            LOGGER.info("Received signal %s, shutting down", signum)
            self.shutdown_event.set()

        signal.signal(signal.SIGTERM, _handle_signal)
        signal.signal(signal.SIGINT, _handle_signal)

    def run(self) -> None:
        self._ensure_dirs()
        self._install_signal_handlers()

        handler = QueueingHandler(self.queue)
        for directory in self.settings.watch_dirs:
            self.observer.schedule(handler, str(directory), recursive=True)

        self.observer.start()
        LOGGER.info("Watching directories: %s", ", ".join(str(item) for item in self.settings.watch_dirs))

        self._bootstrap_existing_files()

        with httpx.Client(timeout=self.settings.openrouter_timeout_seconds) as client:
            while not self.shutdown_event.is_set():
                try:
                    path = self.queue.get(timeout=0.8)
                except queue.Empty:
                    continue

                try:
                    process_file(path, self.settings, self.state, client)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("Failed to process %s: %s", path, exc)

        self.observer.stop()
        self.observer.join(timeout=5)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    settings = load_settings()

    LOGGER.info("file-watcher starting")
    LOGGER.info("cockpit_api_url=%s", settings.cockpit_api_url)
    LOGGER.info("chunking_strategy=%s", settings.chunking_strategy)

    runner = Runner(settings)
    runner.run()


if __name__ == "__main__":
    main()
