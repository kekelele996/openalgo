/**
 * Presenting the server's input review in the deployment's own settings.
 *
 * The review itself is the server's: the program file is the one document that
 * states what an input is, and the check runs again before every start. This
 * file only turns its machine reasons into the words and counts the panel
 * shows, so the same wording cannot drift between the lists and the sentence
 * under them.
 */

import type { InputReview, InputReviewItem } from '@/api/openscriptRunner'

/**
 * What each machine reason means to the trader.
 *
 * The first three bucket a value without action: it is kept, it follows the
 * script's own value, or it has to be entered again. Everything else is the
 * specific thing that has to be re-entered, named so the trader knows whether
 * to type a different number, pick from the new choices or set something that
 * is now blank.
 */
export function reasonCopy(reason: string | undefined): string {
  switch (reason) {
    case 'removed':
      return 'No longer declared; the saved value is dropped and the script’s new text decides.'
    case 'added':
      return 'New in this script; it runs on the script’s own default until you set it.'
    case 'changed':
      return 'The declaration changed since this was set. The same value can mean another quantity or period — set it again to keep it knowingly.'
    case 'kind':
      return 'Not the kind of value this input takes now.'
    case 'bounds':
      return 'Outside the minimum or maximum the script declares now.'
    case 'options':
      return 'No longer one of the choices the script offers.'
    case 'empty':
      return 'This needs a value; it was saved blank.'
    case 'missing':
      return 'Required by the new script, with no default to fall back on.'
    default:
      return 'Needs review against the script as it stands now.'
  }
}

/** Whether an item needs the trader to set a value before the confirming save. */
export function mustReenter(item: InputReviewItem): boolean {
  return item.reason !== 'removed' && item.reason !== 'added'
}

export interface ReviewCounts {
  keep: number
  defaults: number
  reset: number
}

/** The three counts the sentence under the lists is built from. */
export function reviewCounts(review: InputReview): ReviewCounts {
  return {
    keep: review.keep.length,
    defaults: review.default.length,
    reset: review.reset.length,
  }
}

/**
 * The one sentence under the lists.
 *
 * It says what happened and what the two saves are, and it is written when the
 * review says something rather than always: a deployment already reconciled
 * shows no review at all.
 */
export function reviewSentence(review: InputReview): string {
  const { keep, defaults, reset } = reviewCounts(review)
  const parts: string[] = []
  if (keep) parts.push(`${keep} can be kept`)
  if (defaults) parts.push(`${defaults} will follow the script's own value`)
  if (reset) parts.push(`${reset} must be re-entered`)
  const joined = parts.join('; ') || 'Nothing carries over unchanged'
  return `${joined}. Nothing has been saved yet; save once more to confirm.`
}
