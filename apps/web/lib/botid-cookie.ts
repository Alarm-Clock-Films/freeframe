/**
 * The BotID verification cookie: a five-minute HS256 JWT the /botid/verify
 * route sets once BotID has classified the visitor as human.
 *
 * Server-only (it reads BOTID_COOKIE_SECRET). The API checks the same cookie
 * in Python (apps/api/services/botid.py), so the name, claims and lifetime
 * here must match it. Follows Vercel's "verify on page load" pattern:
 * https://vercel.com/docs/botid/verify-on-page-load
 */
import { SignJWT, jwtVerify } from 'jose'
import { cookies } from 'next/headers'

export const BOTID_COOKIE_NAME = '__Host-botid-verification'
const ISSUER = 'freeframe'
const AUDIENCE = 'botid-verification'
const PURPOSE = 'botid-verification'
const LIFETIME_SECONDS = 5 * 60

/** The signing key, or null when BotID is off (BOTID_COOKIE_SECRET unset). */
export function signingKey(): Uint8Array | null {
  const secret = process.env.BOTID_COOKIE_SECRET?.trim()
  if (!secret) return null
  if (!/^[0-9a-f]{64}$/i.test(secret)) {
    throw new Error('BOTID_COOKIE_SECRET must be 32 random bytes in hex (openssl rand -hex 32)')
  }
  return Uint8Array.from(Buffer.from(secret, 'hex'))
}

export async function hasVerificationCookie(key: Uint8Array): Promise<boolean> {
  const token = (await cookies()).get(BOTID_COOKIE_NAME)?.value
  if (!token) return false
  try {
    const { payload } = await jwtVerify(token, key, {
      algorithms: ['HS256'],
      issuer: ISSUER,
      audience: AUDIENCE,
      requiredClaims: ['iat', 'exp'],
      maxTokenAge: `${LIFETIME_SECONDS}s`,
    })
    return payload.purpose === PURPOSE
  } catch {
    return false
  }
}

export async function issueVerificationCookie(key: Uint8Array): Promise<void> {
  const token = await new SignJWT({ purpose: PURPOSE })
    .setProtectedHeader({ alg: 'HS256' })
    .setIssuer(ISSUER)
    .setAudience(AUDIENCE)
    .setIssuedAt()
    .setExpirationTime(`${LIFETIME_SECONDS}s`)
    .sign(key)

  ;(await cookies()).set(BOTID_COOKIE_NAME, token, {
    httpOnly: true,
    secure: true,
    sameSite: 'strict',
    path: '/',
    maxAge: LIFETIME_SECONDS,
  })
}
