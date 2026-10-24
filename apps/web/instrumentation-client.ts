import { initBotId } from 'botid/client/core'
import { BOTID_ENABLED, BOTID_VERIFY_PATH } from './lib/botid'

// Vercel BotID. Only /botid/verify is protected on the client: it runs the
// server check once and hands the API a short-lived cookie, because the API
// (a Python service) cannot call checkBotId() itself. See lib/botid.ts.
if (BOTID_ENABLED) {
  initBotId({
    protect: [
      {
        path: BOTID_VERIFY_PATH,
        method: 'POST',
        advancedOptions: { checkLevel: 'basic' },
      },
    ],
  })
}
