/** Shared prev/next pager for the long song tables, styled like the schedule
 * sidebar's pager. Renders nothing when everything fits on one page. */
interface PagerProps {
  page: number;
  totalRows: number;
  pageSize: number;
  onPage: (page: number) => void;
}

export default function Pager({ page, totalRows, pageSize, onPage }: PagerProps) {
  const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
  if (totalPages <= 1) return null;
  const safe = Math.min(page, totalPages - 1);
  const lo = safe * pageSize + 1;
  const hi = Math.min(totalRows, (safe + 1) * pageSize);

  return (
    <div className="sched-pager">
      <span className="mono" style={{ color: "var(--text-muted)", fontSize: 11 }}>
        {lo}–{hi} of {totalRows}
      </span>
      <div style={{ display: "flex", gap: 6 }}>
        <button className="pager-btn" disabled={safe === 0} onClick={() => onPage(safe - 1)}>
          ‹ Prev
        </button>
        <button
          className="pager-btn"
          disabled={safe === totalPages - 1}
          onClick={() => onPage(safe + 1)}
        >
          Next ›
        </button>
      </div>
    </div>
  );
}
