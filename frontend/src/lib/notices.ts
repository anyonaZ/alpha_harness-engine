/**
 * News worth a system notification, as the backend sends it on the `notices` topic: a task
 * ended, BRAIN's daily limit was reached, BRAIN refused the session. The backend compares
 * each state with the last itself (its `notices.py`), so a window hidden in the tray only
 * listens and fetches nothing.
 */

import { useEffect, useRef, useSyncExternalStore } from 'react'
import { telemetry } from './ws'

interface EndedTask {
  id: number
  status: string
  name: string
  message: string
}

export type Notice =
  | { kind: 'tasks_ended'; tasks: EndedTask[] }
  | { kind: 'daily_limit' }
  | { kind: 'session_lost' }

const ENDED: Record<string, string> = { COMPLETE: 'finished', PAUSED: 'paused', FAILED: 'failed' }

/** The title and body a notice shows, or null for one this build does not know. */
export function describe(notice: Notice): { title: string; body: string } | null {
  switch (notice.kind) {
    case 'tasks_ended': {
      const ended = notice.tasks.filter((task) => ENDED[task.status])
      const [only] = ended
      if (ended.length === 1 && only)
        return { title: `${only.name} ${ENDED[only.status]}`, body: only.message }
      if (ended.length > 1) {
        // One notice for a batch, as when several tasks end in the same moment.
        const shown = ended.slice(0, 3).map((t) => `${t.name} ${ENDED[t.status]}`)
        const more = ended.length > 3 ? `, and ${ended.length - 3} more` : ''
        return { title: `${ended.length} tasks ended`, body: `${shown.join(', ')}${more}.` }
      }
      return null
    }
    case 'daily_limit':
      return {
        title: "BRAIN's daily simulation limit is reached",
        body: 'Queued simulations wait for the reset at midnight US Eastern.',
      }
    case 'session_lost':
      return {
        title: 'BRAIN is not accepting your session',
        body: 'Sending is paused until the session is renewed or you sign in again.',
      }
    default:
      return null
  }
}

/** Calls `show` with each notice's title and body as it arrives. */
export function useNotices(show: (title: string, body: string) => void): void {
  const latest = useRef(show)
  useEffect(() => {
    latest.current = show
  })
  useEffect(
    () =>
      telemetry.subscribe('notices', (payload) => {
        const shown = describe(payload as Notice)
        if (shown) latest.current(shown.title, shown.body)
      }),
    [],
  )
}

/** Whether what is on screen already says it: the page is in front and has focus. */
export const onScreen = () => document.visibilityState === 'visible' && document.hasFocus()

// ── In a browser tab ────────────────────────────────────────────────────────────────────

/** Notifications exist here: a browser tab on localhost, which counts as a secure origin. */
export const browserNotifications = typeof window !== 'undefined' && 'Notification' in window

type Permission = NotificationPermission | 'unsupported'

const permissionListeners = new Set<() => void>()

/** The tab's notification permission, kept current as it is asked for. */
export function useNotificationPermission(): Permission {
  return useSyncExternalStore(
    (listener) => {
      permissionListeners.add(listener)
      return () => permissionListeners.delete(listener)
    },
    () => (browserNotifications ? Notification.permission : 'unsupported'),
  )
}

/** Asks for permission. Call from a click: browsers ignore or bury a prompt raised unasked. */
export async function askNotificationPermission(): Promise<void> {
  if (!browserNotifications) return
  await Notification.requestPermission()
  for (const listener of permissionListeners) listener()
}

/** In a browser tab, the same notices the desktop app shows, once the tab is allowed to. */
export function BrowserNotifications() {
  useNotices((title, body) => {
    if (!browserNotifications || Notification.permission !== 'granted' || onScreen()) return
    new Notification(title, { body, tag: title })
  })
  return null
}
