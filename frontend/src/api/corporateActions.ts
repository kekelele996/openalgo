/**
 * Corporate action notices (stock split / bonus issue) for the sandbox
 * account. Session-based routes on the sandbox blueprint; CSRF is attached by
 * the webClient interceptor.
 */
import { webClient } from './client'

export type CorporateActionType = 'split' | 'bonus'

export interface CorporateActionBookRow {
  kind: 'position' | 'holding'
  user_id: string
  product?: string
  settlement_date?: string | null
  quantity_before: number
  quantity_after: number
  average_price_before: number
  average_price_after: number
  rounded?: boolean
  pnl_after?: number | null
}

export interface CorporateActionOrderRow {
  kind: 'order'
  user_id: string
  orderid: string
  action: string
  price_type: string
  quantity_before: number
  quantity_after: number
  price_before: number | null
  price_after: number | null
  trigger_price_before: number | null
  trigger_price_after: number | null
  rounded?: boolean
}

export interface CorporateActionGttLeg {
  leg_number: number
  action: string
  pricetype: string
  quantity_before: number
  quantity_after: number
  price_before: number
  price_after: number
  trigger_price_before: number
  trigger_price_after: number
  rounded?: boolean
}

export interface CorporateActionGttRow {
  kind: 'gtt'
  user_id: string
  gtt_id: string
  trigger_type: string
  last_price_before: number
  last_price_after: number
  legs: CorporateActionGttLeg[]
}

export interface CorporateActionSkip {
  kind: string
  id: string
  user_id?: string
  reason: string
}

export interface CorporateActionCounts {
  accounts: number
  positions: number
  holdings: number
  orders: number
  gtts: number
}

export interface CorporateActionPreview {
  notice_id: string
  symbol: string
  exchange: string
  action_type: CorporateActionType
  ratio_from: number
  ratio_to: number
  effective_date: string
  factor: number
  already_applied: boolean
  applied_at?: string | null
  applied_by?: string | null
  counts: CorporateActionCounts
  positions: CorporateActionBookRow[]
  holdings: CorporateActionBookRow[]
  orders: CorporateActionOrderRow[]
  gtts: CorporateActionGttRow[]
  skipped: CorporateActionSkip[]
}

export interface CorporateActionApplySummary {
  notice_lines: number
  newly_applied: number
  already_applied: number
  accounts_affected: number
  positions: number
  holdings: number
  orders: number
  gtts: number
}

export interface PreviewResponse {
  status: string
  data: { actions: CorporateActionPreview[] }
}

export interface ApplyResponse {
  status: string
  message?: string
  data: {
    actions: CorporateActionPreview[]
    summary: CorporateActionApplySummary
  }
}

export const corporateActionsApi = {
  preview(csv: string) {
    return webClient
      .post<PreviewResponse>('/sandbox/api/corporate-actions/preview', { csv })
      .then((res) => res.data)
  },
  apply(csv: string) {
    return webClient
      .post<ApplyResponse>('/sandbox/api/corporate-actions/apply', { csv })
      .then((res) => res.data)
  },
}
