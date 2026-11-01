/**
 * The wording around the server's input review.
 *
 * The check itself is the server's and is tested there; this file guards the
 * one thing the panel owns: the reason a trader reads for each outcome, and the
 * sentence that says what the confirming save does.
 */

import { describe, expect, it } from 'vitest'
import type { InputReview } from '@/api/openscriptRunner'
import { mustReenter, reasonCopy, reviewCounts, reviewSentence } from './inputReview'

const item = (key: string, reason: string) => ({ key, label: key, kind: 'number', reason })

const REVIEW: InputReview = {
  script: 'x.oscript',
  changed: true,
  keep: [item('length', '') as InputReview['keep'][number]],
  default: [item('legacy', 'removed'), item('factor', 'added')],
  reset: [item('mode', 'options'), item('qty', 'changed')],
  clean: { length: 20, qty: 1 },
}

describe('the reason wording', () => {
  it('names what happened for every reason the server sends', () => {
    for (const reason of [
      'removed',
      'added',
      'changed',
      'kind',
      'bounds',
      'options',
      'empty',
      'missing',
    ]) {
      expect(reasonCopy(reason).length).toBeGreaterThan(0)
    }
    expect(reasonCopy('removed')).toContain('dropped')
    expect(reasonCopy('changed')).toContain('another quantity or period')
    expect(reasonCopy(undefined)).toContain('Needs review')
  })

  it('says only removed and added follow the script without being re-entered', () => {
    expect(mustReenter(item('a', 'removed'))).toBe(false)
    expect(mustReenter(item('b', 'added'))).toBe(false)
    expect(mustReenter(item('c', 'changed'))).toBe(true)
    expect(mustReenter(item('d', 'bounds'))).toBe(true)
  })
})

describe('the sentence', () => {
  it('counts all three buckets and says nothing was saved yet', () => {
    const sentence = reviewSentence(REVIEW)
    expect(sentence).toContain('1 can be kept')
    expect(sentence).toContain('2 will follow')
    expect(sentence).toContain('2 must be re-entered')
    expect(sentence).toContain('Nothing has been saved')
  })

  it('counts from a real review shape', () => {
    expect(reviewCounts(REVIEW)).toEqual({ keep: 1, defaults: 2, reset: 2 })
  })

  it('still says something when every bucket is empty', () => {
    const empty: InputReview = {
      script: 'x.oscript',
      changed: false,
      keep: [],
      default: [],
      reset: [],
      clean: {},
    }
    expect(reviewSentence(empty)).toContain('Nothing carries over')
  })
})
