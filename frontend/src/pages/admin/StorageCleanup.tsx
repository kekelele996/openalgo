import {
  AlertTriangle,
  CheckCircle2,
  HardDrive,
  Info,
  Loader2,
  RefreshCw,
  Trash2,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { adminApi } from '@/api/admin'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { CleanupCategory, CleanupJob, CleanupPreview } from '@/types/admin'
import { showToast } from '@/utils/toast'

function formatBytes(bytes: number): string {
  if (!bytes) return '0 MB'
  const mb = bytes / (1024 * 1024)
  if (mb < 1024) return `${mb.toFixed(mb < 10 ? 1 : 0)} MB`
  return `${(mb / 1024).toFixed(2)} GB`
}

function unitLabel(unit: string, count: number): string {
  if (unit === 'files') return count === 1 ? '1 file' : `${count} files`
  if (unit === 'lines') return count === 1 ? '1 line' : `${count} lines`
  return count === 1 ? '1 row' : `${count} rows`
}

function SkipCell({ category }: { category: CleanupCategory }) {
  if (!category.skips.length) {
    return <span className="text-sm text-muted-foreground">None</span>
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          variant="outline"
          className="cursor-help border-amber-500/60 text-amber-700 dark:text-amber-300"
        >
          <AlertTriangle className="mr-1 h-3 w-3" />
          {category.skip_count} in use
        </Badge>
      </TooltipTrigger>
      <TooltipContent className="max-w-sm space-y-2">
        {category.skips.map((skip) => (
          <div key={skip.reason}>
            <p className="font-medium">{skip.reason}</p>
            {skip.samples.length > 0 && (
              <ul className="mt-1 list-disc pl-4 text-xs opacity-80">
                {skip.samples.map((sample) => (
                  <li key={sample} className="truncate">
                    {sample}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </TooltipContent>
    </Tooltip>
  )
}

function JobProgress({ job }: { job: CleanupJob }) {
  const failed = Object.entries(job.categories).filter(([, state]) => state.status === 'failed')
  return (
    <div className="mt-4 space-y-3 rounded-md border p-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">
          {job.status === 'complete'
            ? 'Cleanup complete'
            : job.status === 'partial'
              ? 'Cleanup stopped on some categories'
              : 'Cleaning up...'}
        </span>
        <span className="text-sm text-muted-foreground">
          {formatBytes(job.totals_freed.bytes)} freed so far
        </span>
      </div>
      <div className="space-y-2">
        {Object.entries(job.categories).map(([id, state]) => {
          const plan = job.plans[id]
          const done =
            state.kind === 'files'
              ? (state.items ?? []).filter((item) => item.status === 'done').length
              : (state.deleted ?? 0)
          const planned = state.planned_delete
          const percent = planned ? Math.round((done / planned) * 100) : 100
          return (
            <div key={id} className="space-y-1">
              <div className="flex items-center justify-between gap-2 text-sm">
                <span className="flex items-center gap-1.5">
                  {state.status === 'done' ? (
                    <CheckCircle2 className="h-3.5 w-3.5 text-green-600" />
                  ) : state.status === 'failed' ? (
                    <AlertTriangle className="h-3.5 w-3.5 text-amber-600" />
                  ) : (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  )}
                  {plan?.label ?? id}
                </span>
                <span className="text-muted-foreground">
                  {done}/{planned}
                </span>
              </div>
              <Progress
                value={percent}
                className={state.status === 'failed' ? 'bg-amber-100 dark:bg-amber-950' : ''}
              />
              {state.error && (
                <p className="text-xs text-amber-700 dark:text-amber-300">{state.error}</p>
              )}
            </div>
          )
        })}
      </div>
      {failed.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Finished batches have been kept. Retry to clear the remaining records.
        </p>
      )}
    </div>
  )
}

export default function StorageCleanup() {
  const [preview, setPreview] = useState<CleanupPreview | null>(null)
  const [days, setDays] = useState<Record<string, number>>({})
  const [isLoading, setIsLoading] = useState(true)
  const [isCalculating, setIsCalculating] = useState(false)
  const [job, setJob] = useState<CleanupJob | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [isAdvancing, setIsAdvancing] = useState(false)
  const advancingRef = useRef(false)

  const loadPreview = useCallback(async (daysToUse?: Record<string, number>) => {
    setIsCalculating(true)
    try {
      const data = await adminApi.getCleanupPreview(daysToUse)
      setPreview(data)
      setDays((current) => ({ ...data.days, ...current }))
    } catch {
      showToast.error('Could not load the cleanup preview. Check the server logs.')
    } finally {
      setIsLoading(false)
      setIsCalculating(false)
    }
  }, [])

  useEffect(() => {
    loadPreview()
    adminApi
      .getLatestCleanupJob()
      .then((latest) => {
        if (latest && (latest.status === 'running' || latest.status === 'partial')) {
          setJob(latest)
        }
      })
      .catch(() => undefined)
  }, [loadPreview])

  const advanceJob = useCallback(async (jobId: string) => {
    if (advancingRef.current) return
    advancingRef.current = true
    setIsAdvancing(true)
    try {
      const updated = await adminApi.runCleanupNext(jobId, 20)
      setJob(updated)
    } catch {
      showToast.error('Could not continue the cleanup. Finished batches were kept.')
    } finally {
      advancingRef.current = false
      setIsAdvancing(false)
    }
  }, [])

  // Drive the running job one chunk at a time until it settles. A partial
  // result waits for an explicit retry: the operator may want to read why a
  // batch failed before trying again.
  useEffect(() => {
    if (!job || job.status !== 'running') return
    const timer = setTimeout(() => {
      void advanceJob(job.job_id)
    }, 400)
    return () => clearTimeout(timer)
  }, [job, advanceJob])

  const handleRecalculate = () => {
    void loadPreview(days)
  }

  const handleConfirm = async () => {
    setConfirmOpen(false)
    try {
      const created = await adminApi.createCleanupJob(days)
      setJob(created)
    } catch {
      showToast.error('Could not start the cleanup. Nothing was deleted.')
    }
  }

  const handleRetry = () => {
    if (job) void advanceJob(job.job_id)
  }

  const handleFinishedDismiss = async () => {
    setJob(null)
    await loadPreview(days)
  }

  const busy = isLoading || isCalculating
  const jobActive = job?.status === 'running'
  const totalDelete = preview?.totals.delete_count ?? 0

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <HardDrive className="h-4 w-4" />
          Storage cleanup
        </CardTitle>
        <CardDescription>
          Preview old logs and run records before deleting them. Records of running strategies and
          orders still being checked are skipped.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {preview?.disk && (
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-muted-foreground">
            {preview.disk.log && (
              <span>
                Log volume:{' '}
                <strong className="text-foreground">{preview.disk.log.free_gb} GB free</strong> (
                {preview.disk.log.used_percent}% used)
              </span>
            )}
            {preview.disk.db && (
              <span>
                Database volume:{' '}
                <strong className="text-foreground">{preview.disk.db.free_gb} GB free</strong> (
                {preview.disk.db.used_percent}% used)
              </span>
            )}
          </div>
        )}

        {isLoading ? (
          <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Reading record counts...
          </div>
        ) : (
          preview && (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="min-w-48">Records</TableHead>
                    <TableHead className="w-28">Keep for</TableHead>
                    <TableHead className="text-right">Total</TableHead>
                    <TableHead className="text-right">Delete</TableHead>
                    <TableHead className="text-right">Kept</TableHead>
                    <TableHead>Skipped, in use</TableHead>
                    <TableHead className="text-right">Frees (est.)</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {preview.categories.map((category) => (
                    <TableRow key={category.id}>
                      <TableCell>
                        <div className="font-medium">{category.label}</div>
                        <div className="text-xs text-muted-foreground">{category.description}</div>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          <Input
                            type="number"
                            min={1}
                            max={3650}
                            className="h-8 w-16"
                            value={days[category.id] ?? category.retention_days}
                            disabled={jobActive}
                            aria-label={`Days to keep ${category.label}`}
                            onChange={(event) =>
                              setDays((current) => ({
                                ...current,
                                [category.id]: Number(event.target.value),
                              }))
                            }
                            onKeyDown={(event) => {
                              if (event.key === 'Enter') handleRecalculate()
                            }}
                          />
                          <span className="text-xs text-muted-foreground">days</span>
                        </div>
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {unitLabel(category.unit, category.total_count)}
                      </TableCell>
                      <TableCell className="text-right font-medium tabular-nums">
                        {unitLabel(category.unit, category.delete_count)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums text-muted-foreground">
                        {unitLabel(category.unit, category.keep_count)}
                      </TableCell>
                      <TableCell>
                        <SkipCell category={category} />
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {formatBytes(category.delete_bytes)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )
        )}

        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="text-sm">
            {preview && !isLoading && (
              <span>
                Total space to free: <strong>{formatBytes(preview.totals.delete_bytes)}</strong>
                {preview.totals.skip_count > 0 && (
                  <span className="text-muted-foreground">
                    {' '}
                    ({preview.totals.skip_count} item(s) skipped, still in use)
                  </span>
                )}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={handleRecalculate}
              disabled={busy || jobActive}
            >
              <RefreshCw className={`mr-1.5 h-4 w-4 ${isCalculating ? 'animate-spin' : ''}`} />
              Recalculate
            </Button>
            <Button
              size="sm"
              variant="destructive"
              onClick={() => setConfirmOpen(true)}
              disabled={busy || totalDelete === 0 || job?.status === 'running'}
            >
              <Trash2 className="mr-1.5 h-4 w-4" />
              Clean up selected records
            </Button>
          </div>
        </div>

        <p className="flex items-start gap-1.5 text-xs text-muted-foreground">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          Deletion runs in small batches. If a batch fails, finished batches stay deleted and a
          retry only processes the remainder. SQLite reuses freed space for new records.
        </p>

        {job && <JobProgress job={job} />}

        {job?.status === 'partial' && (
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={handleFinishedDismiss}>
              Close
            </Button>
            <Button size="sm" onClick={handleRetry} disabled={isAdvancing}>
              <RefreshCw className={`mr-1.5 h-4 w-4 ${isAdvancing ? 'animate-spin' : ''}`} />
              Retry remaining
            </Button>
          </div>
        )}

        {job?.status === 'complete' && (
          <div className="flex justify-end">
            <Button size="sm" onClick={handleFinishedDismiss}>
              Done
            </Button>
          </div>
        )}
      </CardContent>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete old records?</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p>
                  This will permanently delete{' '}
                  <strong>{preview?.totals.delete_count.toLocaleString()}</strong> old record(s),
                  freeing about{' '}
                  <strong>{preview && formatBytes(preview.totals.delete_bytes)}</strong>.
                </p>
                {preview && preview.totals.skip_count > 0 && (
                  <p>
                    {preview.totals.skip_count} item(s) still in use (running strategies or orders
                    being checked) will be skipped.
                  </p>
                )}
                <p>The cleanup runs in batches and can be retried if a batch fails.</p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(event) => {
                event.preventDefault()
                void handleConfirm()
              }}
              className="bg-destructive text-white hover:bg-destructive/90"
            >
              Delete old records
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}
