import { useEffect, useMemo, useState } from "react";

type CommandFeedItem = {
  kind: string;
  headline: string;
  subline: string;
  timestamp: string;
  severity: string;
};

type DashboardAccount = {
  id: number;
  user_id: string;
  provider: string;
  google_email: string;
  display_name: string | null;
  status: string;
  has_refresh_token: boolean;
  scopes: string[];
};

type DashboardOverview = {
  posture: string;
  counts: Record<string, number>;
  metrics: Record<string, number>;
  circuit_breakers: {
    openrouter?: {
      state?: string;
      failures?: number;
      open_until_epoch?: number | null;
    };
  };
  accounts: DashboardAccount[];
  command_feed: CommandFeedItem[];
};

const API_BASE = "/api";

function formatRelative(iso: string): string {
  if (!iso) {
    return "n/a";
  }
  const when = new Date(iso).getTime();
  if (Number.isNaN(when)) {
    return iso;
  }
  const diffMinutes = Math.round((Date.now() - when) / 60000);
  if (diffMinutes < 1) {
    return "now";
  }
  if (diffMinutes < 60) {
    return `${diffMinutes}m fa`;
  }
  const diffHours = Math.round(diffMinutes / 60);
  if (diffHours < 24) {
    return `${diffHours}h fa`;
  }
  const diffDays = Math.round(diffHours / 24);
  return `${diffDays}d fa`;
}

function postureLabel(posture: string): string {
  if (posture === "degraded") {
    return "Degraded";
  }
  if (posture === "attention") {
    return "Attention";
  }
  return "Nominal";
}

function App() {
  const [data, setData] = useState<DashboardOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const response = await fetch(`${API_BASE}/dashboard/overview`, {
          headers: {
            Accept: "application/json",
          },
        });
        if (!response.ok) {
          throw new Error(`dashboard_http_${response.status}`);
        }
        const payload = (await response.json()) as DashboardOverview;
        if (cancelled) {
          return;
        }
        setData(payload);
        setError(null);
      } catch (err) {
        if (cancelled) {
          return;
        }
        setError(err instanceof Error ? err.message : "dashboard_fetch_failed");
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    };

    void load();
    const interval = window.setInterval(() => {
      void load();
    }, 15000);

    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  const counts = data?.counts ?? {};
  const metrics = data?.metrics ?? {};
  const accounts = data?.accounts ?? [];
  const feed = data?.command_feed ?? [];
  const openrouterState = data?.circuit_breakers?.openrouter?.state ?? "unknown";

  const topCards = useMemo(
    () => [
      {
        label: "Signal Vault",
        value: counts.external_documents ?? 0,
        detail: "documenti normalizzati",
      },
      {
        label: "Event Ledger",
        value: counts.raw_events ?? 0,
        detail: "eventi con provenance",
      },
      {
        label: "Human Channels",
        value: counts.message_events ?? 0,
        detail: "messaggi ingestiti",
      },
      {
        label: "Google Accounts",
        value: counts.google_accounts ?? 0,
        detail: "account collegati",
      },
    ],
    [counts],
  );

  const urgencyStrip = useMemo(
    () => [
      {
        title: "OpenRouter",
        tone: openrouterState === "open" ? "critical" : "normal",
        value: openrouterState,
      },
      {
        title: "Dead Letters",
        tone: (counts.dead_letter_events ?? 0) > 0 ? "warning" : "normal",
        value: String(counts.dead_letter_events ?? 0),
      },
      {
        title: "Gmail Sync Runs",
        tone: "normal",
        value: String(metrics.google_sync_gmail_runs_total ?? 0),
      },
      {
        title: "Drive Sync Runs",
        tone: "normal",
        value: String(metrics.google_sync_drive_runs_total ?? 0),
      },
      {
        title: "Calendar Sync Runs",
        tone: "normal",
        value: String(metrics.google_sync_calendar_runs_total ?? 0),
      },
    ],
    [counts.dead_letter_events, metrics, openrouterState],
  );

  return (
    <div className="shell">
      <div className="shell__backdrop" />
      <main className="frame">
        <section className="hero">
          <div className="hero__meta">
            <span className="hero__eyebrow">Personal Operating Headquarters</span>
            <span className={`hero__posture hero__posture--${data?.posture ?? "unknown"}`}>
              {postureLabel(data?.posture ?? "unknown")}
            </span>
          </div>
          <div className="hero__body">
            <div>
              <h1>Command-level visibility for your life as if it were a high-stakes company.</h1>
              <p>
                Questa plancia unifica memoria, integrazioni e stato macchina. Quando il planner
                sarà completo, qui vedrai direzione, priorità e rischio operativo in tempo reale.
              </p>
            </div>
            <div className="hero__callout">
              <span>Current doctrine</span>
              <strong>
                {accounts.length > 0
                  ? "Sources connected. Build the decision layer next."
                  : "Connect sources. Increase signal density before planning."}
              </strong>
            </div>
          </div>
        </section>

        <section className="metrics-grid">
          {topCards.map((card) => (
            <article className="metric-card" key={card.label}>
              <span className="metric-card__label">{card.label}</span>
              <strong className="metric-card__value">{card.value}</strong>
              <span className="metric-card__detail">{card.detail}</span>
            </article>
          ))}
        </section>

        <section className="urgency-strip">
          {urgencyStrip.map((item) => (
            <article className={`urgency-pill urgency-pill--${item.tone}`} key={item.title}>
              <span>{item.title}</span>
              <strong>{item.value}</strong>
            </article>
          ))}
        </section>

        <section className="board">
          <div className="panel panel--tall">
            <div className="panel__header">
              <div>
                <span className="panel__eyebrow">Operational feed</span>
                <h2>Recent command flow</h2>
              </div>
              <span className="panel__badge">{feed.length} entries</span>
            </div>
            {loading ? <p className="panel__empty">Loading command feed...</p> : null}
            {error ? <p className="panel__empty panel__empty--error">{error}</p> : null}
            {!loading && !error && feed.length === 0 ? (
              <p className="panel__empty">Ancora nessun segnale operativo.</p>
            ) : null}
            <div className="feed">
              {feed.map((item, index) => (
                <article className={`feed__item feed__item--${item.severity}`} key={`${item.headline}-${index}`}>
                  <div className="feed__dot" />
                  <div className="feed__content">
                    <header>
                      <strong>{item.headline}</strong>
                      <time>{formatRelative(item.timestamp)}</time>
                    </header>
                    <p>{item.subline || "no details"}</p>
                  </div>
                </article>
              ))}
            </div>
          </div>

          <div className="board__column">
            <article className="panel">
              <div className="panel__header">
                <div>
                  <span className="panel__eyebrow">Source mesh</span>
                  <h2>Connected accounts</h2>
                </div>
                <span className="panel__badge">{accounts.length}</span>
              </div>
              {accounts.length === 0 ? (
                <p className="panel__empty">
                  Nessun account Google ancora collegato. La UI e` pronta; il prossimo passo e`
                  autorizzare Gmail, Drive e Calendar.
                </p>
              ) : (
                <div className="account-list">
                  {accounts.map((account) => (
                    <article className="account-list__item" key={account.id}>
                      <div>
                        <strong>{account.display_name || account.google_email}</strong>
                        <p>{account.google_email}</p>
                      </div>
                      <div className="account-list__meta">
                        <span>{account.status}</span>
                        <span>{account.has_refresh_token ? "refresh ok" : "no refresh"}</span>
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </article>

            <article className="panel">
              <div className="panel__header">
                <div>
                  <span className="panel__eyebrow">Doctrine board</span>
                  <h2>Next build priorities</h2>
                </div>
              </div>
              <ol className="directive-list">
                <li>
                  Collegare il primo account Google e densificare il segnale con Gmail, Drive e
                  Calendar.
                </li>
                <li>
                  Aggiungere il vero planner giornaliero con task normalizzati, scadenze e energy
                  routing.
                </li>
                <li>
                  Consolidare la vista persona/progetto/obbligo per far emergere direzione
                  esecutiva, non solo raccolta dati.
                </li>
              </ol>
            </article>
          </div>
        </section>
      </main>
    </div>
  );
}

export default App;
