"use client";

import { type ReactNode, useId, useState } from "react";

export default function TermTooltip({
  name,
  description,
  children,
}: {
  name: string;
  description: string;
  children: ReactNode;
}) {
  const id = useId();
  const [dismissed, setDismissed] = useState(false);
  return (
    <button
      type="button"
      className="term"
      data-term={name}
      aria-describedby={id}
      aria-labelledby={`${id}-label`}
      data-dismissed={dismissed}
      onFocus={() => setDismissed(false)}
      onMouseEnter={() => setDismissed(false)}
      onKeyDown={(event) => {
        if (event.key === "Escape") setDismissed(true);
      }}
    >
      <span id={`${id}-label`}>{children}</span>
      <span className="term-mark" aria-hidden="true">
        ?
      </span>
      <span id={id} className="term-tip" role="tooltip">
        {description}
      </span>
    </button>
  );
}
