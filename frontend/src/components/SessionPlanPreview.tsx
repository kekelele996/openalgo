import { AlertTriangle, Clock, LogIn, LogOut } from 'lucide-react'
import { formatIst, type SessionPlan } from '@/lib/sessionSchedule'

/**
 * The concrete next-start / mandatory-stop panel shared by the Flow start
 * node and the /python schedule page. Read-only: it renders what the
 * calendar resolved, it does not edit the schedule.
 */
export function SessionPlanPreview({
  plan,
  loading,
}: {
  plan: SessionPlan | null
  loading?: boolean
}) {
  if (loading) {
    return <p className="text-[10px] text-muted-foreground">Checking exchange sessions…</p>
  }
  if (!plan) return null

  if (plan.blocked) {
    return (
      <div
        role="alert"
        className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-[11px] text-destructive"
      >
        <div className="flex items-center gap-1.5 font-medium">
          <AlertTriangle className="h-3.5 w-3.5" />
          {plan.reason === 'calendar_unavailable'
            ? 'Trading calendar unavailable — the schedule will not start'
            : 'No startable session for this schedule'}
        </div>
        <p className="mt-1 text-destructive/90">{plan.message}</p>
      </div>
    )
  }

  const start = formatIst(plan.next_start)
  const stop = formatIst(plan.must_stop)
  const special = plan.is_special || plan.kind === 'evening'

  return (
    <div className="space-y-1 rounded-md border border-border bg-muted/40 p-2 text-[11px]">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-muted-foreground">
          <LogIn className="h-3.5 w-3.5" />
          Next can start
        </span>
        <span className="font-medium">{start ?? '—'}</span>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-muted-foreground">
          <LogOut className="h-3.5 w-3.5" />
          Must stop by
        </span>
        <span className="font-medium">{stop ?? 'Never (24/7)'}</span>
      </div>
      <div className="flex items-start justify-between gap-2 border-t border-border pt-1">
        <span className="flex items-center gap-1.5 text-muted-foreground">
          <Clock className="h-3.5 w-3.5" />
          Session
        </span>
        <span
          className={`text-right font-medium ${special ? 'text-amber-600 dark:text-amber-400' : ''}`}
        >
          {plan.kind === 'unrestricted' ? 'Market-hours gating off' : plan.session_label}
        </span>
      </div>
    </div>
  )
}
