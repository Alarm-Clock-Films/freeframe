// @vitest-environment node
// jose's typed-array checks fail across jsdom's realm; this route runs in Node.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { jwtVerify } from 'jose'

const jar = new Map<string, string>()
const set = vi.fn((name: string, value: string, _opts?: Record<string, unknown>) => { jar.set(name, value) })
vi.mock('next/headers', () => ({
  cookies: async () => ({
    get: (name: string) => (jar.has(name) ? { name, value: jar.get(name)! } : undefined),
    set,
  }),
}))

const checkBotId = vi.fn()
vi.mock('botid/server', () => ({ checkBotId: (...args: unknown[]) => checkBotId(...args) }))

import { POST } from '../route'
import { BOTID_COOKIE_NAME } from '@/lib/botid-cookie'

const SECRET = 'ab'.repeat(32)
const ORIGIN = 'https://frame.example.com'
const human = { isHuman: true, isBot: false, isVerifiedBot: false, bypassed: false }

function post(origin: string | null = ORIGIN) {
  const headers = new Headers()
  if (origin) headers.set('origin', origin)
  return POST(new Request(`${ORIGIN}/botid/verify`, { method: 'POST', headers }))
}

beforeEach(() => {
  jar.clear()
  set.mockClear()
  checkBotId.mockReset()
  vi.stubEnv('BOTID_COOKIE_SECRET', SECRET)
  vi.spyOn(console, 'error').mockImplementation(() => {})
})
afterEach(() => vi.unstubAllEnvs())

describe('POST /botid/verify', () => {
  it('issues a cookie the API will accept when BotID sees a person', async () => {
    checkBotId.mockResolvedValue(human)
    const res = await post()
    expect(res.status).toBe(204)
    expect(checkBotId).toHaveBeenCalledWith({ advancedOptions: { checkLevel: 'basic' } })

    const [name, token, opts] = set.mock.calls[0]
    expect(name).toBe(BOTID_COOKIE_NAME)
    expect(opts).toMatchObject({ httpOnly: true, secure: true, sameSite: 'strict', path: '/', maxAge: 300 })
    // The claims apps/api/services/botid.py checks.
    const { payload, protectedHeader } = await jwtVerify(token, Buffer.from(SECRET, 'hex'), {
      issuer: 'freeframe',
      audience: 'botid-verification',
    })
    expect(protectedHeader.alg).toBe('HS256')
    expect(payload.purpose).toBe('botid-verification')
    expect(payload.exp! - payload.iat!).toBe(300)
  })

  it.each([
    ['a bot', { ...human, isHuman: false, isBot: true }],
    ['a verified bot', { ...human, isVerifiedBot: true }],
    ['a bypassed check', { ...human, bypassed: true }],
  ])('refuses %s and sets no cookie', async (_label, result) => {
    checkBotId.mockResolvedValue(result)
    expect((await post()).status).toBe(403)
    expect(set).not.toHaveBeenCalled()
  })

  it('refuses a cross-origin request before running BotID', async () => {
    expect((await post('https://evil.example')).status).toBe(403)
    expect((await post(null)).status).toBe(403)
    expect(checkBotId).not.toHaveBeenCalled()
  })

  it('reuses a valid cookie without another check or a longer life', async () => {
    checkBotId.mockResolvedValue(human)
    await post()
    set.mockClear()
    checkBotId.mockClear()

    expect((await post()).status).toBe(204)
    expect(checkBotId).not.toHaveBeenCalled()
    expect(set).not.toHaveBeenCalled()
  })

  it('does nothing when BotID is off', async () => {
    vi.stubEnv('BOTID_COOKIE_SECRET', '')
    expect((await post()).status).toBe(204)
    expect(checkBotId).not.toHaveBeenCalled()
    expect(set).not.toHaveBeenCalled()
  })

  it('answers 503, not a cookie, when BotID itself fails', async () => {
    checkBotId.mockRejectedValue(new Error('upstream down'))
    expect((await post()).status).toBe(503)
    expect(set).not.toHaveBeenCalled()
  })
})
