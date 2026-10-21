import type { ReactNode } from "react";

export function Panel({
  title,
  right,
  children,
  flush = false,
}: {
  title?: string;
  right?: ReactNode;
  children: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className="panel">
      {title && (
        <header className="panel__head">
          <h2>{title}</h2>
          {right && <span className="right">{right}</span>}
        </header>
      )}
      {flush ? children : <div className="panel__body">{children}</div>}
    </section>
  );
}

export function MetaStrip({ items }: { items: { k: string; v: ReactNode; tone?: string }[] }) {
  return (
    <div className="strip">
      {items.map((item) => (
        <div className="strip__cell" key={item.k}>
          <div className="strip__k">{item.k}</div>
          <div className="strip__v tnum" style={item.tone ? { color: item.tone } : undefined}>
            {item.v}
          </div>
        </div>
      ))}
    </div>
  );
}

export function Bar({ value, tone }: { value: number; tone?: "amber" | "green" }) {
  return (
    <div className={`bar ${tone ? `bar--${tone}` : ""}`}>
      <i style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
    </div>
  );
}

export function Notice({
  tone = "info",
  children,
}: {
  tone?: "info" | "warn" | "error";
  children: ReactNode;
}) {
  return <div className={`notice notice--${tone}`}>{children}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Kbd({ children }: { children: ReactNode }) {
  return <span className="kbd">{children}</span>;
}
