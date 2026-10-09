interface EmptyStateProps {
  title: string;
  message: string;
}

export function EmptyState({ title, message }: EmptyStateProps) {
  return (
    <section className="card" data-testid="empty-state">
      <div className="card-title">{title}</div>
      <p className="muted">{message}</p>
    </section>
  );
}
