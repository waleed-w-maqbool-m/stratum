"use client";

import { useState } from "react";

import type { ReviewItem } from "@/lib/api";

import {
  consequence,
  editable,
  expression,
  facts,
  headline,
  impactWord,
  kindLabel,
  reasons,
  short,
} from "./describe";

type Json = Record<string, unknown>;

/**
 * One inference, laid out as a case to decide: the claim, why Stratum believes it,
 * what accepting it will change, and the numbers. The kinds with structure worth
 * seeing (codes, the parts of a mixed table, a column that means two things) get a
 * table rather than prose.
 */
export function Finding({
  item,
  decision,
  editing,
  busy,
  onDecide,
  onEdit,
  onCancelEdit,
}: {
  item: ReviewItem;
  decision?: string;
  editing: boolean;
  busy: boolean;
  onDecide: (action: "accept" | "reject" | "edit", value?: unknown) => void;
  onEdit: () => void;
  onCancelEdit: () => void;
}) {
  const why = reasons(item);
  const numbers = facts(item);
  const sql = expression(item);
  const [raw, setRaw] = useState(false);

  return (
    <article aria-labelledby="finding-claim" className="min-w-0">
      <p className="text-[13px] text-[var(--accent)]">{kindLabel(item.kind)}</p>
      <h2 id="finding-claim" className="mt-1 text-[clamp(1.25rem,2.2vw,1.6rem)] leading-snug">
        {headline(item)}
      </h2>
      <p className="mt-1 break-all font-mono text-[12px] text-[var(--fg-muted)]">{short(item.target)}</p>

      {editing ? (
        <EditForm item={item} busy={busy} onSave={(value) => onDecide("edit", value)} onCancel={onCancelEdit} />
      ) : (
        <>
          <Structure item={item} />

          {why.length > 0 && (
            <section aria-labelledby="why" className="mt-6">
              <h3 id="why" className="text-[14px] font-medium">Why Stratum thinks so</h3>
              <ul className="mt-2 space-y-1.5 text-[15px] leading-relaxed">
                {why.map((reason) => (
                  <li key={reason} className="border-l-2 pl-3" style={{ borderColor: "var(--accent-quiet)" }}>
                    {reason}
                  </li>
                ))}
              </ul>
            </section>
          )}

          {numbers.length > 0 && (
            <dl className="mt-5 grid grid-cols-[repeat(auto-fill,minmax(11rem,1fr))] gap-x-6 gap-y-3">
              {numbers.map(([label, value]) => (
                <div key={label}>
                  <dt className="text-[13px] text-[var(--fg-muted)]">{label}</dt>
                  <dd className="tabular mt-0.5 text-[15px]">{value}</dd>
                </div>
              ))}
            </dl>
          )}

          {sql && (
            <section aria-labelledby="sql-used" className="mt-5">
              <h3 id="sql-used" className="text-[14px] font-medium">What it adds to queries</h3>
              <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words border rule bg-[var(--surface)] p-3 text-[13px]">
                <code className="font-mono">{sql}</code>
              </pre>
            </section>
          )}

          <p className="mt-6 text-[15px] leading-relaxed text-[var(--fg-muted)]">
            {consequence(item)}
          </p>
          <p className="mt-2 text-[13px] text-[var(--fg-muted)]">
            Stratum is <span className="tabular text-[var(--fg)]">{Math.round(item.confidence * 100)}%</span> sure.
            This touches {impactWord(item.impact)}.
          </p>

          <div className="mt-6 flex flex-wrap items-center gap-2">
            <DecisionButton label="Accept" shortcut="A" tone="var(--good)" disabled={busy} onClick={() => onDecide("accept")} />
            <DecisionButton label="Reject" shortcut="R" tone="var(--danger)" disabled={busy} onClick={() => onDecide("reject")} />
            {editable(item) && (
              <DecisionButton label="Edit" shortcut="E" disabled={busy} onClick={onEdit} />
            )}
            {decision && (
              <p role="status" className="ml-2 text-[14px]" style={{ color: decision === "reject" ? "var(--danger)" : "var(--good)" }}>
                {decision === "reject" ? "Rejected" : decision === "edit" ? "Edited and accepted" : "Accepted"}
              </p>
            )}
          </div>

          <button
            type="button"
            onClick={() => setRaw((open) => !open)}
            aria-expanded={raw}
            className="mt-8 text-[13px] text-[var(--fg-muted)] underline decoration-[var(--rule)] underline-offset-4"
          >
            {raw ? "Hide" : "Show"} the detector&apos;s raw output
          </button>
          {raw && (
            <pre className="mt-2 max-h-72 overflow-auto border rule bg-[var(--surface)] p-3 text-[12px]">
              <code className="font-mono">
                {JSON.stringify({ found_by: item.detector, value: item.value, evidence: item.evidence }, null, 2)}
              </code>
            </pre>
          )}
        </>
      )}
    </article>
  );
}

function DecisionButton({
  label,
  shortcut,
  tone,
  disabled,
  onClick,
}: {
  label: string;
  shortcut: string;
  tone?: string;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-keyshortcuts={shortcut.toLowerCase()}
      className="flex items-center gap-2 border px-4 py-2 text-[14px] transition-colors disabled:opacity-40"
      style={{ borderColor: tone ?? "var(--rule)", color: tone ?? "var(--fg)" }}
    >
      {label}
      <kbd className="font-mono text-[11px] opacity-70">{shortcut}</kbd>
    </button>
  );
}

/** The kinds whose value is a small table, shown as one. */
function Structure({ item }: { item: ReviewItem }) {
  const v = (item.value ?? {}) as Json;
  const e = (item.evidence ?? {}) as Json;

  if (item.kind === "enum") {
    const counts = new Map(
      ((e.distribution as [string, number][] | undefined) ?? []).map(([code, n]) => [String(code), n]),
    );
    return (
      <SmallTable
        caption="Codes and what they mean"
        head={["Code", "Meaning", "Rows"]}
        rows={Object.entries(v).map(([code, meaning]) => [code, String(meaning), counts.get(code)?.toLocaleString() ?? ""])}
        mono={[0]}
        numeric={[2]}
      />
    );
  }

  if (item.kind === "god_table") {
    const entities = (v.entities ?? {}) as Record<string, Json>;
    return (
      <SmallTable
        caption={`The kinds of record in ${short(v.table)}`}
        head={[String(v.discriminator), "Looks like", "Rows", "Columns it uses"]}
        rows={Object.entries(entities).map(([code, part]) => [
          code,
          String(part.suggested_entity ?? ""),
          Number(part.rows ?? 0).toLocaleString(),
          ((part.columns as string[]) ?? []).length.toString(),
        ])}
        mono={[0]}
        numeric={[2, 3]}
      />
    );
  }

  if (item.kind === "overloaded") {
    const meanings = (v.meanings ?? {}) as Record<string, Json>;
    return (
      <SmallTable
        caption={`What ${v.column} points at`}
        head={[`When ${v.discriminator} is`, "It refers to", "Rows"]}
        rows={Object.entries(meanings).map(([code, meaning]) => [
          code,
          `${short(meaning.target_table)}.${meaning.target_column ?? ""}`,
          Number(meaning.rows ?? 0).toLocaleString(),
        ])}
        mono={[0, 1]}
        numeric={[2]}
      />
    );
  }

  if (item.kind === "type_mismatch" && v.kind === "cross_table") {
    const forms = (v.storage_forms ?? {}) as Record<string, string[]>;
    return (
      <SmallTable
        caption={`Where "${v.meaning}" is stored, by form`}
        head={["Stored as", "Columns"]}
        rows={Object.entries(forms).map(([form, columns]) => [form.replace(/_/g, " "), columns.map(short).join(", ")])}
        mono={[1]}
      />
    );
  }

  if (item.kind === "quality") {
    const examples = (e.examples ?? {}) as Record<string, string[]>;
    return (
      <SmallTable
        caption="Spellings that mean the same thing"
        head={["Treated as", "Spellings found"]}
        rows={Object.entries(examples).map(([canonical, spellings]) => [canonical, spellings.join(", ")])}
        mono={[0, 1]}
      />
    );
  }

  if (item.kind === "naming" && v.description) {
    return <p className="mt-4 text-[15px] leading-relaxed">{String(v.description)}</p>;
  }

  return null;
}

function SmallTable({
  caption,
  head,
  rows,
  mono = [],
  numeric = [],
}: {
  caption: string;
  head: string[];
  rows: string[][];
  mono?: number[];
  numeric?: number[];
}) {
  if (!rows.length) return null;
  return (
    <div className="mt-5 overflow-x-auto border rule">
      <table className="w-full text-[14px]">
        <caption className="border-b rule px-3 py-2 text-left text-[13px] text-[var(--fg-muted)]">{caption}</caption>
        <thead>
          <tr className="border-b rule text-left">
            {head.map((label, index) => (
              <th
                key={label}
                scope="col"
                className={`px-3 py-1.5 text-[13px] font-normal text-[var(--fg-muted)] ${numeric.includes(index) ? "text-right" : ""}`}
              >
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex} className="border-b rule last:border-0">
              {row.map((cell, index) => (
                <td
                  key={index}
                  className={`px-3 py-1.5 ${mono.includes(index) ? "font-mono text-[13px]" : ""} ${numeric.includes(index) ? "tabular text-right" : ""}`}
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EditForm({
  item,
  busy,
  onSave,
  onCancel,
}: {
  item: ReviewItem;
  busy: boolean;
  onSave: (value: unknown) => void;
  onCancel: () => void;
}) {
  const v = (item.value ?? {}) as Json;
  const [naming, setNaming] = useState({
    logical_name: String(v.logical_name ?? ""),
    description: String(v.description ?? ""),
  });
  const [codes, setCodes] = useState<[string, string][]>(
    item.kind === "enum" ? Object.entries(v).map(([code, meaning]) => [code, String(meaning)]) : [],
  );

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (item.kind === "naming") {
      onSave({ logical_name: naming.logical_name.trim().replace(/\s+/g, "_"), description: naming.description.trim() });
    } else {
      onSave(Object.fromEntries(codes.map(([code, meaning]) => [code, meaning.trim()])));
    }
  };

  const field = "w-full border rule bg-[var(--surface)] px-3 py-2 text-[15px]";

  return (
    <form
      onSubmit={submit}
      onKeyDown={(event) => {
        if (event.key === "Escape") onCancel();
      }}
      className="mt-6 space-y-4"
    >
      {item.kind === "naming" ? (
        <>
          <div>
            <label htmlFor="edit-name" className="text-[14px]">Name</label>
            <input
              id="edit-name"
              autoFocus
              value={naming.logical_name}
              onChange={(event) => setNaming({ ...naming, logical_name: event.target.value })}
              className={`${field} mt-1 font-mono`}
            />
          </div>
          <div>
            <label htmlFor="edit-description" className="text-[14px]">What it holds</label>
            <textarea
              id="edit-description"
              rows={3}
              value={naming.description}
              onChange={(event) => setNaming({ ...naming, description: event.target.value })}
              className={`${field} mt-1`}
            />
          </div>
        </>
      ) : (
        <fieldset>
          <legend className="text-[14px]">What each code means</legend>
          <div className="mt-2 space-y-2">
            {codes.map(([code, meaning], index) => (
              <div key={code} className="grid grid-cols-[5rem_1fr] items-center gap-3">
                <label htmlFor={`code-${index}`} className="font-mono text-[14px]">{code}</label>
                <input
                  id={`code-${index}`}
                  autoFocus={index === 0}
                  value={meaning}
                  onChange={(event) =>
                    setCodes(codes.map((pair, i) => (i === index ? [pair[0], event.target.value] : pair)))
                  }
                  className={field}
                />
              </div>
            ))}
          </div>
        </fieldset>
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={busy}
          className="px-4 py-2 text-[14px] disabled:opacity-40"
          style={{ background: "var(--accent)", color: "var(--on-accent)" }}
        >
          Save and accept
        </button>
        <button type="button" onClick={onCancel} className="border rule px-4 py-2 text-[14px]">
          Cancel
        </button>
      </div>
    </form>
  );
}
