// Shared types and formatting for session-resolved schedules.
//
// Both the Flow start node and the /python schedule page resolve the next
// concrete start and mandatory stop against the exchange calendar
// (regular / special / evening sessions). This is their common wire shape
// and IST-safe rendering.

export type SessionKind = 'regular' | 'special' | 'evening' | 'crypto' | 'unrestricted'

export interface SessionPlan {
  exchange: string
  kind: SessionKind
  blocked: boolean
  startable: boolean
  reason: string | null
  description: string | null
  is_special: boolean
  session_date: string | null
  session_start: string | null
  session_end: string | null
  effective_start: string | null
  effective_end: string | null
  /** ISO IST; the next moment the schedule may start. */
  next_start: string | null
  /** ISO IST; the moment it must stop by. Null for a 24/7 no-stop schedule. */
  must_stop: string | null
  session_label: string
  message: string
}

const IST_FULL = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata',
  weekday: 'short',
  day: '2-digit',
  month: 'short',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

const IST_TIME = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Kolkata',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
})

/** An ISO instant as e.g. "Mon 02 Nov, 09:15 IST"; null for null/unparseable. */
export function formatIst(iso: string | null | undefined): string | null {
  if (!iso) return null
  const value = new Date(iso)
  if (Number.isNaN(value.getTime())) return null
  return `${IST_FULL.format(value)} IST`
}

/** Just the IST HH:MM part. */
export function formatIstTime(iso: string | null | undefined): string | null {
  if (!iso) return null
  const value = new Date(iso)
  if (Number.isNaN(value.getTime())) return null
  return IST_TIME.format(value)
}
