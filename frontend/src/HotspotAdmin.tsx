import { useCallback, useEffect, useState } from "react";
import { HotspotApiError, basicAuth, hotspotApi, saveAuth, savedAuth } from "./hotspotApi";
import type { HotspotActivity, HotspotActivityKind, HotspotBlocked, HotspotClients, HotspotEvent, HotspotLive } from "./types";

const REFRESH_MS = 5000;
// While someone is uploading or compressing, refresh faster so progress moves smoothly.
const LIVE_REFRESH_MS = 2000;
const EVENTS_PAGE = 50;

const bytes = (value: number | null) =>
  value == null
    ? "—"
    : value < 1024
      ? `${value} B`
      : value < 1024 ** 2
        ? `${(value / 1024).toFixed(1)} KB`
        : value < 1024 ** 3
          ? `${(value / 1024 ** 2).toFixed(1)} MB`
          : `${(value / 1024 ** 3).toFixed(2)} GB`;

const duration = (value: number | null) => {
  if (value == null) return "—";
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  if (hours) return `${hours} ч ${minutes} мин`;
  if (minutes) return `${minutes} мин ${value % 60} с`;
  return `${value} с`;
};

const time = (epochSeconds: number) =>
  new Date(epochSeconds * 1000).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

// The label replaces the hostname everywhere; the MAC is the last resort.
const nameOf = (item: { label: string | null; hostname?: string | null; mac: string }) =>
  item.label ?? item.hostname ?? "Без имени";

// Devices outside the hotspot (Ethernet, USB) have no lease, so their IP is the name.
const deviceName = (item: { label: string | null; hostname: string | null; mac: string | null; ip?: string; client?: string }) =>
  item.label ?? item.hostname ?? item.ip ?? item.client ?? "—";

const ACTIONS: Record<HotspotActivityKind, { text: string; tone: "on" | "off" | "info" }> = {
  upload: { text: "загрузил файл", tone: "on" },
  upload_failed: { text: "загрузка не удалась", tone: "off" },
  compressed: { text: "сжал файл", tone: "on" },
  compress_failed: { text: "ошибка сжатия", tone: "off" },
  compress_cancelled: { text: "отменил сжатие", tone: "off" },
  download: { text: "скачал файл", tone: "info" },
  download_archive: { text: "скачал архив", tone: "info" },
  archive_import: { text: "открыл архив", tone: "info" },
  decompress: { text: "распаковал архив", tone: "info" },
};

const liveAction = (item: HotspotLive) =>
  item.kind === "upload" ? "загружает" : item.status === "queued" ? "ждёт сжатия" : "сжимает";

const percent = (item: HotspotLive) => (item.total ? Math.min(100, (item.done / item.total) * 100) : null);

const activityDetails = (item: HotspotActivity) => {
  const parts: string[] = [];
  if (item.kind === "compressed" && item.size != null && item.compressedSize != null) {
    const saved = item.size ? (1 - item.compressedSize / item.size) * 100 : 0;
    parts.push(`→ ${bytes(item.compressedSize)} (${saved >= 0 ? "−" : "+"}${Math.abs(saved).toFixed(1)}%)`);
    if (item.seconds != null) parts.push(`за ${item.seconds < 1 ? `${Math.round(item.seconds * 1000)} мс` : `${item.seconds.toFixed(1)} с`}`);
  }
  if (item.algorithm) parts.push(item.algorithm.toUpperCase());
  // A gzip upload sends fewer bytes over Wi-Fi than the file has.
  if (item.sentBytes != null && item.size != null && item.sentBytes < item.size) parts.push(`передано ${bytes(item.sentBytes)} (gzip)`);
  if (item.kind === "upload_failed" && item.sentBytes != null) parts.push(`передано ${bytes(item.sentBytes)}`);
  if (item.detail) parts.push(item.detail);
  return parts.join("; ") || "—";
};

// rx/tx are counted by the access point: rx is what the device sent, tx what it received.
const Traffic = ({ rx, tx }: { rx: number | null; tx: number | null }) => (
  <span title="↑ отправлено устройством, ↓ получено устройством">
    ↑ {bytes(rx)} ↓ {bytes(tx)}
  </span>
);

function Login({ onLogin, error }: { onLogin: (auth: string) => void; error: string }) {
  const [password, setPassword] = useState("");
  return (
    <main className="share-page">
      <form
        className="share-card"
        onSubmit={(event) => {
          event.preventDefault();
          if (password) onLogin(basicAuth(password));
        }}
      >
        <span className="eyebrow">HOTSPOT ADMIN</span>
        <h2>Вход в панель точки доступа</h2>
        <input
          className="admin-input"
          type="password"
          autoFocus
          autoComplete="current-password"
          placeholder="Пароль администратора"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        <button className="primary full" type="submit" disabled={!password}>
          Войти
        </button>
        {error && <p className="error">{error}</p>}
      </form>
    </main>
  );
}

export default function HotspotAdmin() {
  const [auth, setAuth] = useState<string | null>(savedAuth);
  const [loginError, setLoginError] = useState("");
  const [clients, setClients] = useState<HotspotClients>();
  const [blocked, setBlocked] = useState<HotspotBlocked[]>([]);
  const [firewallError, setFirewallError] = useState<string | null>(null);
  const [events, setEvents] = useState<HotspotEvent[]>([]);
  const [eventsTotal, setEventsTotal] = useState(0);
  const [eventLimit, setEventLimit] = useState(EVENTS_PAGE);
  const [live, setLive] = useState<HotspotLive[]>([]);
  const [activity, setActivity] = useState<HotspotActivity[]>([]);
  const [activityTotal, setActivityTotal] = useState(0);
  const [activityLimit, setActivityLimit] = useState(EVENTS_PAGE);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<string>();

  const logout = useCallback((message = "") => {
    saveAuth(null);
    setAuth(null);
    setLoginError(message);
  }, []);

  const fail = useCallback(
    (cause: unknown) => {
      if (cause instanceof HotspotApiError && cause.status === 401) logout("Неверный пароль.");
      else if (cause instanceof HotspotApiError && cause.status === 404)
        logout("Панель выключена: задайте FILECOMP_HOTSPOT_ADMIN_PASSWORD на сервере.");
      else setError(cause instanceof Error ? cause.message : "Ошибка запроса");
    },
    [logout],
  );

  const refresh = useCallback(async () => {
    if (!auth) return;
    const api = hotspotApi(auth);
    try {
      const [current, block, journal, feed] = await Promise.all([
        api.clients(),
        api.blocked(),
        api.events(eventLimit),
        api.activity(activityLimit),
      ]);
      setClients(current);
      setLive(feed.live);
      setActivity(feed.activity);
      setActivityTotal(feed.total);
      setBlocked(block.blocked);
      setFirewallError(block.error);
      setEvents(journal.events);
      setEventsTotal(journal.total);
      setError("");
    } catch (cause) {
      fail(cause);
    }
  }, [auth, eventLimit, activityLimit, fail]);

  const busy = live.length > 0;
  useEffect(() => {
    if (!auth) return;
    refresh();
    const timer = setInterval(refresh, busy ? LIVE_REFRESH_MS : REFRESH_MS);
    return () => clearInterval(timer);
  }, [auth, refresh, busy]);

  if (!auth) {
    return (
      <Login
        error={loginError}
        onLogin={(value) => {
          saveAuth(value);
          setLoginError("");
          setAuth(value);
        }}
      />
    );
  }

  const api = hotspotApi(auth);
  // Every action shows its own result and then reloads all three tables.
  const act = async (key: string, action: () => Promise<unknown>) => {
    setPending(key);
    setError("");
    try {
      await action();
      await refresh();
    } catch (cause) {
      fail(cause);
    } finally {
      setPending(undefined);
    }
  };
  const rename = (mac: string, current: string | null) => {
    const label = window.prompt(`Метка для ${mac} (пусто — убрать):`, current ?? "");
    if (label !== null) act(`label-${mac}`, () => api.setLabel(mac, label));
  };

  return (
    <main className="admin">
      <header>
        <div className="brand">
          <span className="mark">FC</span>
          <span>Точка доступа</span>
        </div>
        <div className="online">
          <i /> {clients ? `${clients.interface}; обновлено ${clients.updatedAt ? time(clients.updatedAt) : "—"}` : "Загрузка..."}
          <button className="link-button" onClick={() => logout()}>
            Выйти
          </button>
        </div>
      </header>

      {error && <p className="error admin-alert">{error}</p>}
      {clients?.error && <p className="error admin-alert">Не удалось получить список устройств: {clients.error}</p>}
      {firewallError && <p className="error admin-alert">Блокировка не работает: {firewallError}</p>}

      <section className="panel admin-section">
        <span className="eyebrow">ПОДКЛЮЧЕНЫ СЕЙЧАС</span>
        <h2>{clients ? `${clients.clients.length} устр.` : "…"}</h2>
        <div className="table-scroll">
          <table className="admin-table">
            <thead>
              <tr>
                <th>Имя</th>
                <th>MAC</th>
                <th>IP</th>
                <th>Сигнал</th>
                <th>Подключён</th>
                <th>Трафик</th>
                <th>Сейчас</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {clients?.clients.map((client) => (
                <tr key={client.mac}>
                  <td>
                    <b>{nameOf(client)}</b>
                    <button className="link-button" onClick={() => rename(client.mac, client.label)}>
                      метка
                    </button>
                  </td>
                  <td className="mono">{client.mac}</td>
                  <td className="mono">{client.ip ?? "—"}</td>
                  <td className="mono">{client.signal == null ? "—" : `${client.signal} dBm`}</td>
                  <td title={time(client.connectedAt)}>{duration(client.connectedSeconds)}</td>
                  <td className="mono">
                    <Traffic rx={client.rxBytes} tx={client.txBytes} />
                  </td>
                  <td>
                    {live
                      .filter((item) => item.mac === client.mac)
                      .map((item) => (
                        <div key={item.id} className="now-doing">
                          {liveAction(item)} <b>{item.filename}</b>
                          {percent(item) != null && ` ${percent(item)!.toFixed(0)}%`}
                        </div>
                      ))}
                  </td>
                  <td className="actions">
                    <button disabled={!!pending} onClick={() => act(`kick-${client.mac}`, () => api.kick(client.mac))}>
                      Отключить
                    </button>
                    <button
                      className="danger"
                      disabled={!!pending || client.blocked}
                      onClick={() => {
                        if (window.confirm(`Заблокировать ${nameOf(client)} (${client.mac})?`))
                          act(`block-${client.mac}`, () => api.block(client.mac));
                      }}
                    >
                      {client.blocked ? "Заблокирован" : "Заблокировать"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {clients && !clients.clients.length && <div className="empty-metrics">Никто не подключён.</div>}
        </div>
      </section>

      <section className="panel admin-section">
        <span className="eyebrow">СЕЙЧАС НА СЕРВЕРЕ</span>
        <h2>{live.length ? `${live.length} ${live.length === 1 ? "операция" : "операций"}` : "Тихо"}</h2>
        {live.length ? (
          <div className="table-scroll">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Устройство</th>
                  <th>Действие</th>
                  <th>Файл</th>
                  <th>Размер</th>
                  <th>Прогресс</th>
                  <th>Скорость</th>
                </tr>
              </thead>
              <tbody>
                {live.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <b>{deviceName(item)}</b>
                      <small className="mono muted">{item.mac ?? item.client}</small>
                    </td>
                    <td>{liveAction(item)}</td>
                    <td className="filename" title={item.filename}>{item.filename}</td>
                    <td className="mono">{bytes(item.size)}</td>
                    <td className="progress-cell">
                      <progress value={item.done} max={Math.max(item.total ?? 0, 1)} />
                      <span className="mono">
                        {bytes(item.done)}
                        {item.total ? ` / ${bytes(item.total)} (${percent(item)!.toFixed(0)}%)` : ""}
                      </span>
                    </td>
                    <td className="mono">{item.bytesPerSecond ? `${bytes(Math.round(item.bytesPerSecond))}/s` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-metrics">Сейчас никто ничего не загружает и не сжимает.</div>
        )}
      </section>

      <section className="panel admin-section">
        <div className="panel-head">
          <div>
            <span className="eyebrow">АКТИВНОСТЬ</span>
            <h2>{activityTotal} записей</h2>
          </div>
          <button
            className="danger"
            disabled={!!pending || !activityTotal}
            onClick={() => {
              if (window.confirm(`Удалить все ${activityTotal} записей активности? Это нельзя отменить.`))
                act("clear-activity", () => api.clearActivity());
            }}
          >
            Очистить активность
          </button>
        </div>
        <div className="table-scroll">
          <table className="admin-table">
            <thead>
              <tr>
                <th>Время</th>
                <th>Устройство</th>
                <th>Действие</th>
                <th>Файл</th>
                <th>Размер</th>
                <th>Подробности</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {activity.map((item) => (
                <tr key={item.id}>
                  <td>{time(item.at)}</td>
                  <td>
                    <b>{deviceName(item)}</b>
                    <small className="mono muted">{item.mac ?? item.ip}</small>
                  </td>
                  <td>
                    <span className={`event-tag ${ACTIONS[item.kind]?.tone ?? "info"}`}>{ACTIONS[item.kind]?.text ?? item.kind}</span>
                  </td>
                  <td className="filename" title={item.filename ?? ""}>{item.filename ?? "—"}</td>
                  <td className="mono">{bytes(item.size)}</td>
                  <td className="details">{activityDetails(item)}</td>
                  <td className="actions">
                    <button
                      className="link-button"
                      disabled={!!pending}
                      onClick={() => act(`activity-${item.id}`, () => api.deleteActivity(item.id))}
                    >
                      удалить
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!activity.length && <div className="empty-metrics">Пока никто ничего не загружал и не скачивал.</div>}
        </div>
        {activity.length < activityTotal && (
          <button className="primary full" onClick={() => setActivityLimit((limit) => limit + EVENTS_PAGE)}>
            Показать ещё
          </button>
        )}
      </section>

      <section className="panel admin-section">
        <span className="eyebrow">ЗАБЛОКИРОВАННЫЕ</span>
        <h2>{blocked.length} MAC</h2>
        {blocked.length ? (
          <div className="table-scroll">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>Имя</th>
                  <th>MAC</th>
                  <th>Заблокирован</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {blocked.map((item) => (
                  <tr key={item.mac}>
                    <td>
                      <b>{item.label ?? "—"}</b>
                      <button className="link-button" onClick={() => rename(item.mac, item.label)}>
                        метка
                      </button>
                    </td>
                    <td className="mono">{item.mac}</td>
                    <td>{time(item.blockedAt)}</td>
                    <td className="actions">
                      <button disabled={!!pending} onClick={() => act(`unblock-${item.mac}`, () => api.unblock(item.mac))}>
                        Разблокировать
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-metrics">Блок-лист пуст.</div>
        )}
      </section>

      <section className="panel admin-section">
        <div className="panel-head">
          <div>
            <span className="eyebrow">ЖУРНАЛ</span>
            <h2>{eventsTotal} записей</h2>
          </div>
          <button
            className="danger"
            disabled={!!pending || !eventsTotal}
            onClick={() => {
              if (window.confirm(`Удалить все ${eventsTotal} записей журнала? Это нельзя отменить.`))
                act("clear", () => api.clearEvents());
            }}
          >
            Очистить журнал
          </button>
        </div>
        <div className="table-scroll">
          <table className="admin-table">
            <thead>
              <tr>
                <th>Время</th>
                <th>Событие</th>
                <th>Имя</th>
                <th>MAC</th>
                <th>IP</th>
                <th>Длительность</th>
                <th>Трафик</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {events.map((event) => (
                <tr key={event.id}>
                  <td>{time(event.at)}</td>
                  <td>
                    <span className={`event-tag ${event.kind === "CONNECTED" ? "on" : "off"}`}>
                      {event.kind === "CONNECTED" ? "подключился" : "отключился"}
                    </span>
                  </td>
                  <td>{nameOf(event)}</td>
                  <td className="mono">{event.mac}</td>
                  <td className="mono">{event.ip ?? "—"}</td>
                  <td>{duration(event.duration)}</td>
                  <td className="mono">
                    {event.kind === "DISCONNECTED" ? <Traffic rx={event.rxBytes} tx={event.txBytes} /> : "—"}
                  </td>
                  <td className="actions">
                    <button
                      className="link-button"
                      disabled={!!pending}
                      onClick={() => act(`event-${event.id}`, () => api.deleteEvent(event.id))}
                    >
                      удалить
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!events.length && <div className="empty-metrics">Журнал пуст.</div>}
        </div>
        {events.length < eventsTotal && (
          <button className="primary full" onClick={() => setEventLimit((limit) => limit + EVENTS_PAGE)}>
            Показать ещё
          </button>
        )}
      </section>
    </main>
  );
}
