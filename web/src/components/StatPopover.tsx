import { useEffect, useRef, useState } from "react";

interface StatPopoverProps {
  trigger: React.ReactNode;
  triggerClassName?: string;
  children: React.ReactNode;
}

/** Hover (mouse) or tap (touch) trigger for a small fixed-position stat card.
 *  Mirrors the outside-click-dismiss pattern used by the header search. */
export default function StatPopover({ trigger, triggerClassName, children }: StatPopoverProps) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const anchorRef = useRef<HTMLSpanElement | null>(null);
  const closeTimer = useRef<number | null>(null);
  const supportsHover = useRef(
    typeof window !== "undefined" && window.matchMedia("(hover: hover)").matches,
  ).current;

  const cancelClose = () => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  };
  const scheduleClose = () => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => setOpen(false), 150);
  };
  const openAt = () => {
    const rect = anchorRef.current?.getBoundingClientRect();
    if (!rect) return;
    setPos({ top: rect.bottom + 6, left: rect.left + rect.width / 2 });
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (!anchorRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onScroll = () => setOpen(false);
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open]);

  useEffect(() => () => cancelClose(), []);

  return (
    <span
      ref={anchorRef}
      className={"stat-pop-anchor" + (triggerClassName ? " " + triggerClassName : "")}
      onMouseEnter={supportsHover ? () => { cancelClose(); openAt(); } : undefined}
      onMouseLeave={supportsHover ? scheduleClose : undefined}
      onClick={(e) => {
        e.stopPropagation();
        if (open) setOpen(false);
        else openAt();
      }}
    >
      {trigger}
      {open && pos && (
        <div
          className="stat-pop"
          style={{ top: pos.top, left: pos.left }}
          onMouseEnter={supportsHover ? cancelClose : undefined}
          onMouseLeave={supportsHover ? scheduleClose : undefined}
          onClick={(e) => e.stopPropagation()}
        >
          {children}
        </div>
      )}
    </span>
  );
}
