export interface CustodyEvent {
  label: string;
  detail?: string;
}

/** Append-only event list. The connecting rule is the point: nothing is edited. */
export default function CustodyList({ events }: { events: CustodyEvent[] }) {
  if (events.length === 0) {
    return <p className="hint" style={{ padding: "12px 14px", margin: 0 }}>No events recorded yet.</p>;
  }
  return (
    <ol className="custody">
      {events.map((event, index) => (
        <li key={`${event.label}-${index}`}>
          <span className="custody__dot" aria-hidden />
          <div>
            <b>{event.label}</b>
            {event.detail && <div className="hint">{event.detail}</div>}
          </div>
        </li>
      ))}
    </ol>
  );
}
