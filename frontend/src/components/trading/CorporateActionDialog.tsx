import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  FileSpreadsheet,
  Info,
  Loader2,
  RefreshCw,
  Upload,
} from 'lucide-react'
import { useRef, useState } from 'react'
import {
  type CorporateActionApplySummary,
  type CorporateActionPreview,
  corporateActionsApi,
} from '@/api/corporateActions'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import { showToast } from '@/utils/toast'

type Phase = 'input' | 'preview' | 'result'

const EXAMPLE_CSV = [
  'notice_id,symbol,exchange,action_type,ratio_from,ratio_to,effective_date',
  'CA-2026-RELIANCE,RELIANCE,NSE,split,1,5,2026-11-03',
  '# bonus: ratio_to free shares for every ratio_from held (1:1 doubles the holding)',
  'CA-2026-TCS,TCS,BSE,bonus,1,1,2026-11-03',
].join('\n')

const inr = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
})

function fmtPrice(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : inr.format(value)
}

function actionLabel(action: CorporateActionPreview): string {
  if (action.action_type === 'split') {
    return `Split ${action.ratio_from}:${action.ratio_to} (×${action.factor})`
  }
  return `Bonus ${action.ratio_from}:${action.ratio_to} (×${action.factor})`
}

function BeforeAfter({
  before,
  after,
  price,
  changed,
}: {
  before: number | string
  after: number | string
  price?: boolean
  changed?: boolean
}) {
  const format = (v: number | string) => (price && typeof v === 'number' ? fmtPrice(v) : String(v))
  return (
    <span
      className={
        changed === false ? 'font-mono text-muted-foreground' : 'font-mono whitespace-nowrap'
      }
    >
      {format(before)}
      <ArrowRight className="inline-block h-3 w-3 mx-1 text-muted-foreground" />
      <span className="font-semibold">{format(after)}</span>
    </span>
  )
}

function ActionPreviewCard({ action }: { action: CorporateActionPreview }) {
  const totalAffected =
    action.counts.positions + action.counts.holdings + action.counts.orders + action.counts.gtts

  return (
    <div className="rounded-lg border p-4 space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="font-semibold">
          {action.symbol} · {action.exchange}
        </Badge>
        <span className="text-sm font-medium">{actionLabel(action)}</span>
        <span className="text-xs text-muted-foreground">effective {action.effective_date}</span>
        {action.already_applied && (
          <Badge className="bg-amber-500/15 text-amber-700 border-amber-500/30 hover:bg-amber-500/15">
            Already applied
          </Badge>
        )}
      </div>

      {action.already_applied && (
        <Alert className="bg-amber-500/10 border-amber-500/30 py-2">
          <Info className="h-4 w-4 text-amber-600" />
          <AlertDescription className="text-amber-700 dark:text-amber-400 text-xs">
            This notice was already imported{action.applied_at ? ` on ${action.applied_at}` : ''}.
            Its stored result is shown below; importing it again will not change any data.
          </AlertDescription>
        </Alert>
      )}

      {totalAffected === 0 ? (
        <p className="text-sm text-muted-foreground py-2">
          No holdings, positions, resting orders or active GTTs on this instrument are affected.
        </p>
      ) : null}

      {action.positions.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Open positions ({action.positions.length})
          </h4>
          <div className="overflow-x-auto rounded border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="h-8">Account</TableHead>
                  <TableHead className="h-8">Product</TableHead>
                  <TableHead className="h-8 text-right">Quantity</TableHead>
                  <TableHead className="h-8 text-right">Avg cost</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {action.positions.map((p, i) => (
                  <TableRow key={`pos-${i}`}>
                    <TableCell className="py-1.5 text-xs">{p.user_id}</TableCell>
                    <TableCell className="py-1.5">{p.product}</TableCell>
                    <TableCell className="py-1.5 text-right">
                      <BeforeAfter before={p.quantity_before} after={p.quantity_after} />
                      {p.rounded && (
                        <span className="ml-1 text-[10px] text-amber-600">rounded</span>
                      )}
                    </TableCell>
                    <TableCell className="py-1.5 text-right">
                      <BeforeAfter
                        before={p.average_price_before}
                        after={p.average_price_after}
                        price
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}

      {action.holdings.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Holdings ({action.holdings.length})
          </h4>
          <div className="overflow-x-auto rounded border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="h-8">Account</TableHead>
                  <TableHead className="h-8">Settled</TableHead>
                  <TableHead className="h-8 text-right">Quantity</TableHead>
                  <TableHead className="h-8 text-right">Avg cost</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {action.holdings.map((h, i) => (
                  <TableRow key={`hold-${i}`}>
                    <TableCell className="py-1.5 text-xs">{h.user_id}</TableCell>
                    <TableCell className="py-1.5 text-xs">{h.settlement_date}</TableCell>
                    <TableCell className="py-1.5 text-right">
                      <BeforeAfter before={h.quantity_before} after={h.quantity_after} />
                      {h.rounded && (
                        <span className="ml-1 text-[10px] text-amber-600">rounded</span>
                      )}
                    </TableCell>
                    <TableCell className="py-1.5 text-right">
                      <BeforeAfter
                        before={h.average_price_before}
                        after={h.average_price_after}
                        price
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}

      {action.orders.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Pending orders to re-price ({action.orders.length})
          </h4>
          <div className="overflow-x-auto rounded border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="h-8">Order</TableHead>
                  <TableHead className="h-8">Type</TableHead>
                  <TableHead className="h-8 text-right">Qty</TableHead>
                  <TableHead className="h-8 text-right">Limit</TableHead>
                  <TableHead className="h-8 text-right">Trigger</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {action.orders.map((o, i) => (
                  <TableRow key={`ord-${i}`}>
                    <TableCell className="py-1.5 text-xs">
                      <div>{o.orderid}</div>
                      <div className="text-[10px] text-muted-foreground">{o.user_id}</div>
                    </TableCell>
                    <TableCell className="py-1.5 text-xs">
                      {o.action} {o.price_type}
                    </TableCell>
                    <TableCell className="py-1.5 text-right">
                      <BeforeAfter before={o.quantity_before} after={o.quantity_after} />
                    </TableCell>
                    <TableCell className="py-1.5 text-right">
                      <BeforeAfter
                        before={o.price_before ?? '—'}
                        after={o.price_after ?? '—'}
                        price
                      />
                    </TableCell>
                    <TableCell className="py-1.5 text-right">
                      <BeforeAfter
                        before={o.trigger_price_before ?? '—'}
                        after={o.trigger_price_after ?? '—'}
                        price
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </div>
      )}

      {action.gtts.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Active GTTs ({action.gtts.length})
          </h4>
          <div className="space-y-2">
            {action.gtts.map((g, i) => (
              <div key={`gtt-${i}`} className="rounded border p-2 space-y-1">
                <div className="flex items-center gap-2 text-xs">
                  <span className="font-medium">{g.gtt_id}</span>
                  <span className="text-muted-foreground">
                    {g.user_id} · {g.trigger_type}
                  </span>
                  <span className="ml-auto">
                    ref price{' '}
                    <BeforeAfter before={g.last_price_before} after={g.last_price_after} price />
                  </span>
                </div>
                {g.legs.map((leg, j) => (
                  <div key={j} className="text-xs text-muted-foreground flex flex-wrap gap-x-4">
                    <span>
                      leg {leg.leg_number} · {leg.action} {leg.pricetype}
                    </span>
                    <span>
                      qty <BeforeAfter before={leg.quantity_before} after={leg.quantity_after} />
                    </span>
                    <span>
                      limit <BeforeAfter before={leg.price_before} after={leg.price_after} price />
                    </span>
                    <span>
                      trigger{' '}
                      <BeforeAfter
                        before={leg.trigger_price_before}
                        after={leg.trigger_price_after}
                        price
                      />
                    </span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}

      {action.skipped.length > 0 && (
        <div className="space-y-1">
          <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Not changed ({action.skipped.length})
          </h4>
          <ul className="text-xs text-muted-foreground space-y-0.5 list-disc pl-4">
            {action.skipped.map((s, i) => (
              <li key={`skip-${i}`}>
                <span className="font-mono">{s.id}</span> ({s.kind}): {s.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  onApplied?: () => void
}

export function CorporateActionDialog({ open, onOpenChange, onApplied }: Props) {
  const [phase, setPhase] = useState<Phase>('input')
  const [csvText, setCsvText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<CorporateActionPreview[]>([])
  const [appliedSummary, setAppliedSummary] = useState<CorporateActionApplySummary | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const reset = () => {
    setPhase('input')
    setCsvText('')
    setBusy(false)
    setError(null)
    setPreview([])
    setAppliedSummary(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const handleFile = (file: File | undefined) => {
    if (!file) return
    const reader = new FileReader()
    reader.onload = () => {
      setCsvText(String(reader.result ?? ''))
      setError(null)
    }
    reader.onerror = () => setError('Could not read the selected file')
    reader.readAsText(file)
  }

  const runPreview = async () => {
    if (!csvText.trim()) {
      setError('Choose a notice file or paste its CSV content first')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const result = await corporateActionsApi.preview(csvText)
      setPreview(result.data.actions)
      setPhase('preview')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to preview the notice')
    } finally {
      setBusy(false)
    }
  }

  const runApply = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await corporateActionsApi.apply(csvText)
      setPreview(result.data.actions)
      setAppliedSummary(result.data.summary)
      setPhase('result')
      if (result.data.summary.newly_applied > 0) {
        showToast.success(
          `Adjusted ${result.data.summary.positions} position(s), ${result.data.summary.holdings} holding(s), ${result.data.summary.orders} order(s), ${result.data.summary.gtts} GTT(s)`,
          'positions'
        )
        onApplied?.()
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to apply the notice')
    } finally {
      setBusy(false)
    }
  }

  const allAlreadyApplied = preview.length > 0 && preview.every((a) => a.already_applied)

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset()
        onOpenChange(next)
      }}
    >
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <FileSpreadsheet className="h-5 w-5" />
            Corporate Action Notice
          </DialogTitle>
          <DialogDescription>
            Import a split or bonus notice. Holdings and costs are converted, and pending orders and
            GTT triggers re-priced, from the effective date onward. The same notice never applies
            twice.
          </DialogDescription>
        </DialogHeader>

        {error && (
          <Alert variant="destructive" className="py-2">
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription className="text-xs">{error}</AlertDescription>
          </Alert>
        )}

        {phase === 'input' && (
          <div className="space-y-3">
            <button
              type="button"
              className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed p-6 text-center cursor-pointer hover:bg-muted/50 transition-colors w-full"
              onClick={() => fileInputRef.current?.click()}
            >
              <Upload className="h-6 w-6 text-muted-foreground" />
              <span className="text-sm font-medium">Choose notice CSV file</span>
              <span className="text-xs text-muted-foreground">
                notice_id, symbol, exchange, action_type, ratio_from, ratio_to, effective_date
              </span>
              <input
                ref={fileInputRef}
                type="file"
                accept=".csv,text/csv,text/plain"
                className="hidden"
                onChange={(e) => handleFile(e.target.files?.[0])}
              />
            </button>

            <Textarea
              value={csvText}
              onChange={(e) => setCsvText(e.target.value)}
              placeholder={'Or paste the notice CSV here…'}
              className="min-h-[140px] font-mono text-xs"
            />

            <div className="flex items-center justify-between">
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0 text-xs"
                onClick={() => setCsvText(EXAMPLE_CSV)}
              >
                Insert an example notice
              </Button>
              <span className="text-[11px] text-muted-foreground">
                action_type is <code>split</code> or <code>bonus</code>; dates are YYYY-MM-DD
              </span>
            </div>
          </div>
        )}

        {phase !== 'input' && (
          <ScrollArea className="max-h-[55vh] pr-3">
            <div className="space-y-4">
              {phase === 'result' && appliedSummary && (
                <Alert
                  className={
                    appliedSummary.newly_applied > 0
                      ? 'bg-emerald-500/10 border-emerald-500/30 py-2'
                      : 'bg-amber-500/10 border-amber-500/30 py-2'
                  }
                >
                  {appliedSummary.newly_applied > 0 ? (
                    <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                  ) : (
                    <Info className="h-4 w-4 text-amber-600" />
                  )}
                  <AlertDescription
                    className={
                      appliedSummary.newly_applied > 0
                        ? 'text-emerald-700 dark:text-emerald-400 text-xs'
                        : 'text-amber-700 dark:text-amber-400 text-xs'
                    }
                  >
                    {appliedSummary.newly_applied > 0
                      ? `Applied ${appliedSummary.newly_applied} of ${appliedSummary.notice_lines} notice line(s) across ${appliedSummary.accounts_affected} account(s).`
                      : 'All imported notice lines were already applied; no data was changed.'}{' '}
                    Positions: {appliedSummary.positions}, holdings: {appliedSummary.holdings},
                    orders: {appliedSummary.orders}, GTTs: {appliedSummary.gtts}.
                  </AlertDescription>
                </Alert>
              )}
              {preview.map((action) => (
                <ActionPreviewCard
                  key={`${action.notice_id}-${action.symbol}-${action.exchange}`}
                  action={action}
                />
              ))}
            </div>
          </ScrollArea>
        )}

        <DialogFooter className="gap-2">
          {phase === 'input' ? (
            <>
              <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
                Cancel
              </Button>
              <Button onClick={runPreview} disabled={busy || !csvText.trim()}>
                {busy ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : null}
                Preview affected accounts
              </Button>
            </>
          ) : phase === 'preview' ? (
            <>
              <Button variant="ghost" onClick={() => setPhase('input')} disabled={busy}>
                Back
              </Button>
              <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
                Cancel
              </Button>
              <Button onClick={runApply} disabled={busy || allAlreadyApplied}>
                {busy ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <RefreshCw className="h-4 w-4 mr-2" />
                )}
                {allAlreadyApplied ? 'Already applied' : 'Confirm & apply'}
              </Button>
            </>
          ) : (
            <Button onClick={() => onOpenChange(false)}>Done</Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default CorporateActionDialog
