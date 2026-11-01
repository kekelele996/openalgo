/**
 * Corporate action notice dialog: import -> before/after preview -> confirm ->
 * result, including the already-applied (idempotent re-import) state.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CorporateActionPreview } from '@/api/corporateActions'
import { fireEvent, render, screen, waitFor } from '@/test/test-utils'
import { CorporateActionDialog } from './CorporateActionDialog'

const apiMocks = vi.hoisted(() => ({
  preview: vi.fn(),
  apply: vi.fn(),
}))

vi.mock('@/api/corporateActions', () => ({
  corporateActionsApi: { preview: apiMocks.preview, apply: apiMocks.apply },
}))

vi.mock('@/utils/toast', () => ({
  showToast: { success: vi.fn(), error: vi.fn() },
}))

const NOTICE =
  'notice_id,symbol,exchange,action_type,ratio_from,ratio_to,effective_date\n' +
  'CA-1,RELIANCE,NSE,split,1,5,2026-10-15\n'

const PREVIEW_PAYLOAD: CorporateActionPreview = {
  notice_id: 'CA-1',
  symbol: 'RELIANCE',
  exchange: 'NSE',
  action_type: 'split',
  ratio_from: 1,
  ratio_to: 5,
  effective_date: '2026-10-15',
  factor: 5,
  already_applied: false,
  applied_at: null,
  counts: { accounts: 1, positions: 1, holdings: 0, orders: 1, gtts: 0 },
  positions: [
    {
      kind: 'position',
      user_id: 'trader-1',
      product: 'CNC',
      quantity_before: 100,
      quantity_after: 500,
      average_price_before: 100,
      average_price_after: 20,
      pnl_after: 1000,
    },
  ],
  holdings: [],
  orders: [
    {
      kind: 'order',
      user_id: 'trader-1',
      orderid: 'ORD-1',
      action: 'SELL',
      price_type: 'SL',
      quantity_before: 100,
      quantity_after: 500,
      price_before: 89,
      price_after: 17.8,
      trigger_price_before: 90,
      trigger_price_after: 18,
    },
  ],
  gtts: [],
  skipped: [],
}

function renderOpen() {
  return render(<CorporateActionDialog open onOpenChange={vi.fn()} onApplied={vi.fn()} />)
}

describe('CorporateActionDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('previews before/after terms and applies on confirm', async () => {
    apiMocks.preview.mockResolvedValue({ status: 'success', data: { actions: [PREVIEW_PAYLOAD] } })
    apiMocks.apply.mockResolvedValue({
      status: 'success',
      data: {
        actions: [PREVIEW_PAYLOAD],
        summary: {
          notice_lines: 1,
          newly_applied: 1,
          already_applied: 0,
          accounts_affected: 1,
          positions: 1,
          holdings: 0,
          orders: 1,
          gtts: 0,
        },
      },
    })
    renderOpen()

    fireEvent.change(screen.getByPlaceholderText(/paste the notice CSV/i), {
      target: { value: NOTICE },
    })
    fireEvent.click(screen.getByRole('button', { name: /Preview affected accounts/ }))

    await waitFor(() => expect(apiMocks.preview).toHaveBeenCalledWith(NOTICE))

    // Before/after quantity and cost for the position, and the re-priced SL.
    expect(await screen.findByText('RELIANCE · NSE')).toBeInTheDocument()
    // 500 appears as the converted quantity of both the position and the order.
    expect(screen.getAllByText('500').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('ORD-1')).toBeInTheDocument()
    expect(screen.getByText(/18\.00/)).toBeInTheDocument() // converted trigger
    expect(screen.getByText(/20\.00/)).toBeInTheDocument() // converted avg cost

    fireEvent.click(screen.getByRole('button', { name: /Confirm & apply/ }))
    await waitFor(() => expect(apiMocks.apply).toHaveBeenCalledWith(NOTICE))
    expect(await screen.findByText(/Applied 1 of 1/)).toBeInTheDocument()
  })

  it('cannot apply an already-imported notice twice', async () => {
    apiMocks.preview.mockResolvedValue({
      status: 'success',
      data: {
        actions: [
          {
            ...PREVIEW_PAYLOAD,
            already_applied: true,
            applied_at: '2026-10-15 09:00:00',
            counts: { accounts: 1, positions: 1, holdings: 0, orders: 1, gtts: 0 },
          },
        ],
      },
    })
    renderOpen()

    fireEvent.change(screen.getByPlaceholderText(/paste the notice CSV/i), {
      target: { value: NOTICE },
    })
    fireEvent.click(screen.getByRole('button', { name: /Preview affected accounts/ }))

    expect(await screen.findByText(/already imported/)).toBeInTheDocument()
    const applyButton = await screen.findByRole('button', { name: /Already applied/ })
    expect(applyButton).toBeDisabled()
    expect(apiMocks.apply).not.toHaveBeenCalled()
  })

  it('shows server parse errors instead of previewing', async () => {
    apiMocks.preview.mockRejectedValue(new Error('Row 2: action_type must be split or bonus'))
    renderOpen()

    fireEvent.change(screen.getByPlaceholderText(/paste the notice CSV/i), {
      target: { value: NOTICE },
    })
    fireEvent.click(screen.getByRole('button', { name: /Preview affected accounts/ }))

    expect(await screen.findByText(/action_type must be split or bonus/)).toBeInTheDocument()
  })
})
