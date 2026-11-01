/**
 * The input review, shown inside a deployment's own settings.
 *
 * No separate page: this is the same form the deployment is saved in, with the
 * review the server ran on the proposed save listed between the fields and the
 * buttons. The three lists are the server's three verdicts:
 *
 * - **Kept** values still satisfy what the saved script declares.
 * - **To default** values name an input the script no longer declares, or one
 *   it added and left at its own value; they are dropped rather than carried.
 * - **Re-enter** rows are the dangerous ones: the value the new script cannot
 *   accept, or a declaration that changed under a still-valid value, which is
 *   how the same number becomes another quantity or period in live trading.
 *
 * Nothing here writes. The first save returned this without writing anything;
 * the one confirming save writes once, and a refusal leaves the old settings as
 * they were.
 */

import type { InputReview, InputReviewItem } from '@/api/openscriptRunner'
import { mustReenter, reasonCopy, reviewSentence } from '@/lib/trading/inputReview'
import { cn } from '@/lib/utils'

function asText(value: InputReviewItem['saved']): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return String(value)
}

function ReviewRow({ one, tone }: { one: InputReviewItem; tone: 'default' | 'reset' }) {
  return (
    <li
      className={cn(
        'flex flex-col gap-0.5 rounded border px-1.5 py-1 text-[10px] leading-snug',
        tone === 'reset'
          ? 'border-destructive/40 bg-destructive/5 text-destructive'
          : 'border-amber-500/40 bg-amber-500/5 text-amber-700 dark:text-amber-400'
      )}
    >
      <span className="font-medium">{one.label}</span>
      <span className="text-muted-foreground">
        {one.reason === 'added'
          ? `default ${asText(one.default)}`
          : `saved ${asText(one.saved)}${
              one.reason === 'removed' ? '' : ` → default ${asText(one.default)}`
            }`}
      </span>
      <span className="text-muted-foreground">{reasonCopy(one.reason)}</span>
    </li>
  )
}

interface Props {
  review: InputReview
  /** Keys the trader has touched since the review came back. */
  touched: ReadonlySet<string>
  busy: boolean
  onConfirm(): void
  onCancel(): void
}

export function InputReviewView({ review, touched, busy, onConfirm, onCancel }: Props) {
  // A row the new script cannot accept has to have been looked at and set again
  // before the confirming save is offered. A "changed" row is consciously kept
  // by touching it even with the same value; the server checks the result.
  const unresolved = review.reset.filter((one) => mustReenter(one) && !touched.has(one.key))

  return (
    <output
      className="flex flex-col gap-1.5 rounded border border-border bg-background p-1.5"
      aria-label="Input review"
    >
      <span className="text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
        Script changed — review inputs
      </span>

      {review.keep.length > 0 && (
        <p className="text-[10px] text-muted-foreground">
          Kept as set: {review.keep.map((one) => one.label).join(', ')}
        </p>
      )}

      {review.default.length > 0 && (
        <ul className="flex flex-col gap-1">
          {review.default.map((one) => (
            <ReviewRow key={`${one.key}-${one.reason}`} one={one} tone="default" />
          ))}
        </ul>
      )}

      {review.reset.length > 0 && (
        <ul className="flex flex-col gap-1">
          {review.reset.map((one) => (
            <ReviewRow key={`${one.key}-${one.reason}`} one={one} tone="reset" />
          ))}
        </ul>
      )}

      <p className="text-[10px] leading-relaxed text-muted-foreground">{reviewSentence(review)}</p>

      <div className="flex gap-1.5">
        <button
          type="button"
          className="h-7 flex-1 rounded border border-border text-[11px] hover:bg-accent disabled:opacity-50"
          disabled={busy || unresolved.length > 0}
          title={
            unresolved.length > 0
              ? `Set ${unresolved.map((one) => one.label).join(', ')} first`
              : undefined
          }
          onClick={onConfirm}
        >
          {busy ? 'Saving' : 'Save reviewed inputs'}
        </button>
        <button
          type="button"
          className="h-7 rounded border border-border px-2 text-[11px] hover:bg-accent"
          onClick={onCancel}
        >
          Cancel
        </button>
      </div>
    </output>
  )
}
