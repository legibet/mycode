/**
 * Chat state management hook.
 * Stores canonical raw messages and derives render messages from them.
 */

import { useCallback, useEffect, useReducer, useRef, useState } from 'react'
import type {
  AttachedFile,
  ChatErrorResponse,
  ChatMessage,
  ChatResponse,
  LocalConfig,
  PermissionRequest,
  RunInfo,
  SessionResponse,
  SessionSummary,
  SessionsResponse,
  StreamEvent,
  ToolRuntime,
} from '../types'
import {
  appendAssistantDelta,
  appendRenderAssistantDelta,
  appendRenderToolUse,
  appendToolResult,
  appendToolUse,
  buildRenderMessages,
  createAssistantMessage,
  createRenderAssistantMessage,
  createRenderUserMessage,
  createUserMessage,
  createUserTextMessage,
  findLatestAssistantIndex,
  updateLatestThinkingDuration,
  updateRenderToolRuntime,
} from '../utils/messages'
import {
  loadActiveSession,
  removeActiveSession,
  saveActiveSession,
} from '../utils/storage'
import {
  isCurrentSendRequest,
  isCurrentWorkspaceRequest,
  resolveInitialSessionId,
} from './sessionSelection'

const DEFAULT_SESSION_TITLE = 'New chat'

interface ChatState {
  rawMessages: ChatMessage[]
  messages: ChatMessage[]
  toolRuntimeById: Record<string, ToolRuntime>
  /** Snapshot of rawMessages taken before the latest optimistic turn.
   * Used by 'rollback' to restore state when the request fails. */
  preTurnRawMessages: ChatMessage[] | null
}

type ChatAction =
  | { type: 'set_messages'; messages: ChatMessage[] }
  | { type: 'start_turn'; content: string; attachments?: AttachedFile[] }
  | { type: 'rewind_and_start_turn'; rewindTo: number; content: string }
  | { type: 'apply_event'; event: StreamEvent }
  | { type: 'rollback' }

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Unknown error'
}

function getErrorDetail(
  data: ChatResponse | ChatErrorResponse,
): ChatErrorResponse['detail'] {
  return 'detail' in data ? data.detail : undefined
}

function getRunFromDetail(detail: ChatErrorResponse['detail']): RunInfo | null {
  return typeof detail === 'object' && detail?.run ? detail.run : null
}

function getMessageFromDetail(
  detail: ChatErrorResponse['detail'],
  fallback: string,
): string {
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail === 'object' && detail.message) {
    return detail.message
  }
  return fallback
}

function createDraftSession(): SessionSummary {
  const id =
    globalThis.crypto?.randomUUID?.() ||
    `draft-${Date.now()}-${Math.random().toString(16).slice(2)}`

  return { id, title: DEFAULT_SESSION_TITLE, isDraft: true }
}

function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case 'set_messages': {
      const rawMessages = Array.isArray(action.messages) ? action.messages : []
      return {
        rawMessages,
        messages: buildRenderMessages(rawMessages),
        toolRuntimeById: {},
        preTurnRawMessages: null,
      }
    }

    case 'start_turn': {
      const sourceIndex = state.rawMessages.length
      const { content, attachments } = action
      return {
        ...state,
        rawMessages: [
          ...state.rawMessages,
          attachments?.length
            ? createUserMessage(content, attachments)
            : createUserTextMessage(content),
          createAssistantMessage([]),
        ],
        messages: [
          ...state.messages,
          createRenderUserMessage(sourceIndex, content, undefined, attachments),
          createRenderAssistantMessage(sourceIndex + 1),
        ],
        preTurnRawMessages: state.rawMessages,
      }
    }

    case 'rewind_and_start_turn': {
      const rawMessages = [
        ...state.rawMessages.slice(0, action.rewindTo),
        createUserTextMessage(action.content),
        createAssistantMessage([]),
      ]
      return {
        rawMessages,
        messages: buildRenderMessages(rawMessages),
        toolRuntimeById: {},
        preTurnRawMessages: state.rawMessages,
      }
    }

    case 'rollback': {
      const snapshot = state.preTurnRawMessages
      if (!snapshot) return state
      return {
        rawMessages: snapshot,
        messages: buildRenderMessages(snapshot),
        toolRuntimeById: {},
        preTurnRawMessages: null,
      }
    }

    case 'apply_event': {
      const event = action.event || {}
      let rawMessages = [...state.rawMessages]
      let messages = state.messages
      const toolRuntimeById = { ...state.toolRuntimeById }

      if (event.type === 'reasoning') {
        rawMessages = appendAssistantDelta(
          rawMessages,
          'thinking',
          event.delta || '',
        )
        const sourceIndex = findLatestAssistantIndex(rawMessages)
        messages = appendRenderAssistantDelta(
          messages,
          'thinking',
          event.delta || '',
          sourceIndex,
        )
      } else if (event.type === 'reasoning_done') {
        const durationMs = event.duration_ms
        if (typeof durationMs === 'number') {
          rawMessages = updateLatestThinkingDuration(rawMessages, durationMs)
          messages = updateLatestThinkingDuration(messages, durationMs)
        }
      } else if (event.type === 'text') {
        rawMessages = appendAssistantDelta(
          rawMessages,
          'text',
          event.delta || '',
        )
        const sourceIndex = findLatestAssistantIndex(rawMessages)
        messages = appendRenderAssistantDelta(
          messages,
          'text',
          event.delta || '',
          sourceIndex,
        )
      } else if (event.type === 'tool_start') {
        const toolCall = event.tool_call || {}
        rawMessages = appendToolUse(rawMessages, toolCall)
        const sourceIndex = findLatestAssistantIndex(rawMessages)
        if (toolCall.id) {
          const runtime: ToolRuntime = {
            pending: true,
            output: '',
            finalOutput: null,
            metadata: null,
            isError: false,
          }
          toolRuntimeById[toolCall.id] = runtime
          messages = appendRenderToolUse(
            messages,
            toolCall,
            sourceIndex,
            runtime,
          )
        } else {
          messages = appendRenderToolUse(messages, toolCall, sourceIndex)
        }
      } else if (event.type === 'tool_output') {
        const toolUseId = event.tool_use_id || ''
        if (toolUseId) {
          const sourceIndex = findLatestAssistantIndex(rawMessages)
          const current = toolRuntimeById[toolUseId] || {
            pending: true,
            output: '',
            finalOutput: null,
            metadata: null,
            isError: false,
          }
          const nextOutput = event.output || ''
          toolRuntimeById[toolUseId] = {
            ...current,
            pending: true,
            output: current.output
              ? `${current.output}\n${nextOutput}`
              : nextOutput,
          }
          messages = updateRenderToolRuntime(
            messages,
            toolUseId,
            toolRuntimeById[toolUseId],
            sourceIndex,
          )
        }
      } else if (event.type === 'tool_done') {
        const toolUseId = event.tool_use_id || ''
        const finalOutput = event.output || ''
        const metadata = event.metadata ?? null
        const isError = Boolean(
          event.is_error ||
            (typeof finalOutput === 'string' &&
              finalOutput.startsWith('error:')),
        )

        if (toolUseId) {
          const current = toolRuntimeById[toolUseId] || {
            pending: false,
            output: '',
            finalOutput: null,
            metadata: null,
            isError: false,
          }
          toolRuntimeById[toolUseId] = {
            ...current,
            pending: false,
            finalOutput,
            metadata,
            isError,
          }
          rawMessages = appendToolResult(
            rawMessages,
            toolUseId,
            finalOutput,
            metadata,
            isError,
          )
          const sourceIndex = findLatestAssistantIndex(rawMessages)
          messages = updateRenderToolRuntime(
            messages,
            toolUseId,
            toolRuntimeById[toolUseId],
            sourceIndex,
            {
              type: 'tool_result',
              tool_use_id: toolUseId,
              output: finalOutput,
              metadata,
              is_error: isError,
            },
          )
        }
      } else if (event.type === 'error') {
        rawMessages = appendAssistantDelta(
          rawMessages,
          'text',
          `\n\n**Error:** ${event.message || 'Unknown'}`,
        )
        const sourceIndex = findLatestAssistantIndex(rawMessages)
        messages = appendRenderAssistantDelta(
          messages,
          'text',
          `\n\n**Error:** ${event.message || 'Unknown'}`,
          sourceIndex,
        )
      }

      return { ...state, rawMessages, messages, toolRuntimeById }
    }
    default:
      return state
  }
}

export function useChat(config: LocalConfig) {
  const [chatState, dispatch] = useReducer(chatReducer, {
    rawMessages: [],
    messages: [],
    toolRuntimeById: {},
    preTurnRawMessages: null,
  })
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [activeSession, setActiveSession] = useState(createDraftSession)
  const [loading, setLoading] = useState(false)
  const [sessionLoading, setSessionLoading] = useState(false)
  const [pendingPermissions, setPendingPermissions] = useState<
    PermissionRequest[]
  >([])
  const initRef = useRef(false)
  const cwdRef = useRef(config.cwd)
  const activeSessionRef = useRef(activeSession)
  const requestTokenRef = useRef(0)
  const pendingRequestTokenRef = useRef(0)
  const sessionRequestTokenRef = useRef(0)
  const streamAbortRef = useRef<AbortController | null>(null)
  const streamTokenRef = useRef(0)
  const activeRunRef = useRef<RunInfo | null>(null)
  const loadSessionRef = useRef<
    | ((
        sessionId: string,
        options?: { requestCwd?: string; requestToken?: number },
      ) => Promise<SessionResponse | null>)
    | null
  >(null)

  const messages = chatState.messages

  const cancelRun = useCallback(async (runId: string) => {
    if (!runId) return

    try {
      await fetch(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
        method: 'POST',
      })
    } catch (e) {
      console.error('Failed to cancel:', e)
    }
  }, [])

  const fetchSessions = useCallback(async (): Promise<SessionSummary[]> => {
    const requestCwd = config.cwd
    try {
      const res = await fetch(
        `/api/sessions?cwd=${encodeURIComponent(requestCwd)}`,
      )
      if (!res.ok) throw new Error('Failed to load sessions')
      const data = (await res.json()) as SessionsResponse
      if (requestCwd !== cwdRef.current) {
        return []
      }
      const savedSessions = data.sessions || []
      const active = activeSessionRef.current
      const sessionsWithDraft =
        active.isDraft &&
        !savedSessions.some((session) => session.id === active.id)
          ? [active, ...savedSessions]
          : savedSessions

      setSessions(sessionsWithDraft)

      const syncedActive = sessionsWithDraft.find(
        (session) => session.id === active.id,
      )
      if (syncedActive && syncedActive !== active) {
        activeSessionRef.current = syncedActive
        setActiveSession(syncedActive)
      }

      return sessionsWithDraft
    } catch (e) {
      console.error('Failed to load sessions:', e)
      return []
    }
  }, [config.cwd])

  const stopStreaming = useCallback(() => {
    streamTokenRef.current += 1
    pendingRequestTokenRef.current = 0
    streamAbortRef.current?.abort()
    streamAbortRef.current = null
    activeRunRef.current = null
    setLoading(false)
    setPendingPermissions([])
  }, [])

  const streamRun = useCallback(
    async (run: RunInfo, sessionId: string, after = 0): Promise<void> => {
      const runId = run?.id
      if (!runId) return

      streamTokenRef.current += 1
      const token = streamTokenRef.current
      streamAbortRef.current?.abort()

      const controller = new AbortController()
      streamAbortRef.current = controller
      activeRunRef.current = run
      setLoading(true)

      const recoverSession = async () => {
        if (
          streamTokenRef.current !== token ||
          activeSessionRef.current.id !== sessionId
        ) {
          return true
        }

        const reload = loadSessionRef.current
        if (!reload) {
          return false
        }

        try {
          await reload(sessionId)
          return true
        } catch (error) {
          console.error('Failed to recover disconnected stream:', error)
          return false
        }
      }

      try {
        const res = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/stream?after=${after}`,
          { signal: controller.signal },
        )
        if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`)
        if (!res.body) throw new Error('Response body is empty')

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        let sawDone = false

        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            const data = line.slice(6)
            if (data === '[DONE]') {
              sawDone = true
              continue
            }

            try {
              const event = JSON.parse(data) as StreamEvent
              if (event.type === 'compact') {
                continue
              }
              if (
                streamTokenRef.current !== token ||
                activeSessionRef.current.id !== sessionId
              ) {
                continue
              }
              if (event.type === 'permission_request') {
                const next: PermissionRequest = {
                  request_id: event.request_id,
                  tool_use_id: event.tool_use_id,
                  tool_name: event.tool_name,
                  preview: event.preview,
                }
                setPendingPermissions((prev) =>
                  prev.some((p) => p.request_id === next.request_id)
                    ? prev
                    : [...prev, next],
                )
                continue
              }
              if (event.type === 'permission_resolved') {
                const requestId = event.request_id
                setPendingPermissions((prev) =>
                  prev.filter((p) => p.request_id !== requestId),
                )
                continue
              }
              dispatch({ type: 'apply_event', event })
            } catch (e) {
              console.error('Parse error:', e)
            }
          }
        }

        if (!sawDone) {
          const recovered = await recoverSession()
          if (recovered) {
            return
          }
        }
      } catch (e) {
        if (!(e instanceof Error) || e.name !== 'AbortError') {
          const recovered = await recoverSession()
          if (
            !recovered &&
            streamTokenRef.current === token &&
            activeSessionRef.current.id === sessionId
          ) {
            dispatch({
              type: 'apply_event',
              event: {
                type: 'error',
                message: 'Stream disconnected. Reload the session to resume.',
              },
            })
          }
        }
      } finally {
        if (streamTokenRef.current === token) {
          streamAbortRef.current = null
          activeRunRef.current = null

          if (activeSessionRef.current.id === sessionId) {
            setLoading(false)
          }

          fetchSessions()
        }
      }
    },
    [fetchSessions],
  )

  const loadSession = useCallback(
    async (
      sessionId: string,
      options?: { requestCwd?: string; requestToken?: number },
    ): Promise<SessionResponse | null> => {
      const requestCwd = options?.requestCwd ?? config.cwd
      const requestToken =
        options?.requestToken ?? sessionRequestTokenRef.current
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`)
      if (!res.ok) throw new Error('Failed to load session')

      const data = (await res.json()) as SessionResponse
      if (
        !isCurrentWorkspaceRequest({
          pendingRequestToken: sessionRequestTokenRef.current,
          requestToken,
          activeCwd: cwdRef.current,
          requestCwd,
        })
      ) {
        return null
      }
      if (!data.session) return null

      activeSessionRef.current = data.session
      setActiveSession(data.session)
      saveActiveSession(requestCwd, data.session.id)
      dispatch({ type: 'set_messages', messages: data.messages || [] })
      setPendingPermissions([])

      const pendingEvents = Array.isArray(data.pending_events)
        ? data.pending_events
        : []
      const replayedPermissions: PermissionRequest[] = []
      for (const event of pendingEvents) {
        if (event?.type === 'compact') continue
        if (event?.type === 'permission_request') {
          replayedPermissions.push({
            request_id: event.request_id,
            tool_use_id: event.tool_use_id,
            tool_name: event.tool_name,
            preview: event.preview,
          })
          continue
        }
        if (event?.type === 'permission_resolved') {
          const requestId = event.request_id
          const index = replayedPermissions.findIndex(
            (p) => p.request_id === requestId,
          )
          if (index >= 0) replayedPermissions.splice(index, 1)
          continue
        }
        dispatch({ type: 'apply_event', event })
      }
      if (replayedPermissions.length) {
        setPendingPermissions(replayedPermissions)
      }

      const run = data.active_run || null
      activeRunRef.current = run

      if (run?.id) {
        const lastSeq = pendingEvents.at(-1)?.seq || 0
        streamRun(run, data.session.id, lastSeq)
      } else {
        setLoading(false)
      }

      return data
    },
    [config.cwd, streamRun],
  )

  useEffect(() => {
    loadSessionRef.current = loadSession
  }, [loadSession])

  const send = useCallback(
    async (input: string, attachments?: AttachedFile[]) => {
      const content = input.trim()
      if ((!content && !attachments?.length) || loading) return

      const sessionId = activeSession.id
      const requestCwd = config.cwd
      const requestToken = requestTokenRef.current + 1

      requestTokenRef.current = requestToken
      pendingRequestTokenRef.current = requestToken

      dispatch({
        type: 'start_turn',
        content,
        ...(attachments?.length ? { attachments } : {}),
      })
      setLoading(true)

      const commonFields = {
        session_id: sessionId,
        provider: config.provider || undefined,
        model: config.model || undefined,
        cwd: config.cwd,
        reasoning_effort:
          config.reasoningEffort && config.reasoningEffort !== 'auto'
            ? config.reasoningEffort
            : undefined,
      }

      // Use structured `input` blocks when attachments are present.
      const body = attachments?.length
        ? {
            ...commonFields,
            input: [
              ...(content ? [{ type: 'text', text: content }] : []),
              ...attachments.map((attachment) =>
                attachment.kind === 'text'
                  ? {
                      type: 'text',
                      text: attachment.text,
                      name: attachment.name,
                      is_attachment: true,
                    }
                  : {
                      type: attachment.kind === 'image' ? 'image' : 'document',
                      data: attachment.data,
                      mime_type: attachment.mime_type,
                      name: attachment.name,
                    },
              ),
            ],
          }
        : { ...commonFields, message: content }

      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })

        const data = (await res.json()) as ChatResponse | ChatErrorResponse
        const isCurrentRequest = isCurrentSendRequest({
          pendingRequestToken: pendingRequestTokenRef.current,
          requestToken,
          activeSessionId: activeSessionRef.current.id,
          sessionId,
          activeCwd: cwdRef.current,
          requestCwd,
        })

        if (!res.ok) {
          const detail = getErrorDetail(data)
          const existingRun = getRunFromDetail(detail)
          if (res.status === 409 && existingRun?.id) {
            if (isCurrentRequest) {
              pendingRequestTokenRef.current = 0
              dispatch({ type: 'rollback' })
              streamRun(existingRun, sessionId, existingRun.last_seq || 0)
            }
            return
          }
          throw new Error(getMessageFromDetail(detail, 'Failed to start task'))
        }

        pendingRequestTokenRef.current = 0

        if (!isCurrentRequest) {
          return
        }

        const chatData = data as ChatResponse

        // Update active session from backend response (has real title, id, etc.)
        if (chatData.session) {
          const session = chatData.session
          activeSessionRef.current = session
          setActiveSession(session)
          saveActiveSession(requestCwd, session.id)
        }

        // Refresh sidebar immediately so title + is_running are visible
        fetchSessions()

        streamRun(chatData.run, sessionId, 0)
      } catch (e) {
        if (
          pendingRequestTokenRef.current === requestToken &&
          activeSessionRef.current.id === sessionId
        ) {
          pendingRequestTokenRef.current = 0
          setLoading(false)
          dispatch({
            type: 'apply_event',
            event: { type: 'error', message: getErrorMessage(e) },
          })
        }
      }
    },
    [activeSession.id, config, fetchSessions, loading, streamRun],
  )

  const rewindAndSend = useCallback(
    async (rewindTo: number, input: string) => {
      const content = input.trim()
      if (!content || loading) return

      const sessionId = activeSession.id
      const requestCwd = config.cwd
      const requestToken = requestTokenRef.current + 1

      requestTokenRef.current = requestToken
      pendingRequestTokenRef.current = requestToken

      dispatch({ type: 'rewind_and_start_turn', rewindTo, content })
      setLoading(true)

      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            session_id: sessionId,
            message: content,
            rewind_to: rewindTo,
            provider: config.provider || undefined,
            model: config.model || undefined,
            cwd: config.cwd,
            reasoning_effort:
              config.reasoningEffort && config.reasoningEffort !== 'auto'
                ? config.reasoningEffort
                : undefined,
          }),
        })

        const data = (await res.json()) as ChatResponse | ChatErrorResponse
        const isCurrentRequest = isCurrentSendRequest({
          pendingRequestToken: pendingRequestTokenRef.current,
          requestToken,
          activeSessionId: activeSessionRef.current.id,
          sessionId,
          activeCwd: cwdRef.current,
          requestCwd,
        })

        if (!res.ok) {
          // Restore original messages on failure (rewind was optimistic).
          if (isCurrentRequest) {
            pendingRequestTokenRef.current = 0
            dispatch({ type: 'rollback' })

            // If 409 (active run), attach to the existing run.
            const detail = getErrorDetail(data)
            const existingRun = getRunFromDetail(detail)
            if (res.status === 409 && existingRun?.id) {
              streamRun(existingRun, sessionId, existingRun.last_seq || 0)
              return
            }

            setLoading(false)
            dispatch({
              type: 'apply_event',
              event: {
                type: 'error',
                message: getMessageFromDetail(detail, 'Failed to start task'),
              },
            })
          }
          return
        }

        pendingRequestTokenRef.current = 0

        if (!isCurrentRequest) return

        const chatData = data as ChatResponse

        if (chatData.session) {
          const session = chatData.session
          activeSessionRef.current = session
          setActiveSession(session)
          saveActiveSession(requestCwd, session.id)
        }

        fetchSessions()
        streamRun(chatData.run, sessionId, 0)
      } catch (e) {
        if (
          pendingRequestTokenRef.current === requestToken &&
          activeSessionRef.current.id === sessionId
        ) {
          pendingRequestTokenRef.current = 0
          dispatch({ type: 'rollback' })
          setLoading(false)
          dispatch({
            type: 'apply_event',
            event: { type: 'error', message: getErrorMessage(e) },
          })
        }
      }
    },
    [activeSession.id, config, fetchSessions, loading, streamRun],
  )

  const cancel = useCallback(() => {
    const runId = activeRunRef.current?.id
    stopStreaming()

    if (!runId) return
    void cancelRun(runId)
  }, [cancelRun, stopStreaming])

  const decidePermission = useCallback(
    async (decision: 'allow' | 'deny') => {
      const head = pendingPermissions[0]
      const runId = activeRunRef.current?.id
      if (!head || !runId) return

      // permission_resolved drives the clear; pre-clearing here would strand
      // the prompt if this POST fails while the server-side wait is still pending.
      try {
        const res = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/decide`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              request_id: head.request_id,
              decision,
            }),
          },
        )
        if (!res.ok) {
          console.error(`Decide POST failed: ${res.status}`)
        }
      } catch (e) {
        console.error('Failed to send decision:', e)
      }
    },
    [pendingPermissions],
  )

  const createSession = useCallback(() => {
    if (sessionLoading) return

    stopStreaming()
    initRef.current = true
    sessionRequestTokenRef.current += 1
    const session = createDraftSession()

    activeSessionRef.current = session
    setActiveSession(session)
    dispatch({ type: 'set_messages', messages: [] })
    // Refresh from server to get accurate is_running, then prepend the new draft
    fetchSessions()
  }, [fetchSessions, sessionLoading, stopStreaming])

  const selectSession = useCallback(
    async (sessionId: string) => {
      if (!sessionId || sessionId === activeSession.id) return

      stopStreaming()
      initRef.current = true
      const requestToken = sessionRequestTokenRef.current + 1
      sessionRequestTokenRef.current = requestToken
      setSessionLoading(true)

      try {
        await loadSession(sessionId, {
          requestCwd: config.cwd,
          requestToken,
        })
        fetchSessions()
      } catch (e) {
        console.error('Failed to load session:', e)
      } finally {
        if (
          isCurrentWorkspaceRequest({
            pendingRequestToken: sessionRequestTokenRef.current,
            requestToken,
            activeCwd: cwdRef.current,
            requestCwd: config.cwd,
          })
        ) {
          setSessionLoading(false)
        }
      }
    },
    [activeSession.id, config.cwd, fetchSessions, loadSession, stopStreaming],
  )

  const deleteSession = useCallback(
    async (sessionId: string) => {
      if (!sessionId) return

      const isDeletingActive = sessionId === activeSession.id
      const deletedIndex = sessions.findIndex(
        (session) => session.id === sessionId,
      )
      const remainingSessions = sessions.filter(
        (session) => session.id !== sessionId,
      )
      const fallbackSession =
        deletedIndex >= 0 ? remainingSessions[deletedIndex] || null : null

      setSessionLoading(true)
      try {
        const res = await fetch(
          `/api/sessions/${encodeURIComponent(sessionId)}`,
          {
            method: 'DELETE',
          },
        )
        if (!res.ok) throw new Error('Failed to delete session')

        if (!isDeletingActive) {
          setSessions(remainingSessions)
          return
        }

        stopStreaming()
        initRef.current = true
        sessionRequestTokenRef.current += 1
        const requestToken = sessionRequestTokenRef.current

        removeActiveSession(config.cwd)
        dispatch({ type: 'set_messages', messages: [] })

        if (fallbackSession && !fallbackSession.isDraft) {
          setSessions(remainingSessions)
          activeSessionRef.current = fallbackSession
          setActiveSession(fallbackSession)
          await loadSession(fallbackSession.id, {
            requestCwd: config.cwd,
            requestToken,
          })
          return
        }

        const draft = createDraftSession()
        activeSessionRef.current = draft
        setActiveSession(draft)
        setSessions([draft])
        setLoading(false)
      } catch (e) {
        console.error('Failed to delete session:', e)
      } finally {
        setSessionLoading(false)
      }
    },
    [activeSession.id, config.cwd, loadSession, sessions, stopStreaming],
  )

  useEffect(() => {
    activeSessionRef.current = activeSession
  }, [activeSession])

  // Single source of init: first mount and any cwd change reset state and
  // reload the workspace's sessions. `loadSession` is read through
  // `loadSessionRef` so this effect doesn't need to re-fire when its identity
  // changes (which would happen on every cwd change).
  useEffect(() => {
    const cwdChanged = cwdRef.current !== config.cwd
    if (cwdChanged) {
      stopStreaming()
      cwdRef.current = config.cwd
      initRef.current = false
      dispatch({ type: 'set_messages', messages: [] })
      setSessions([])
      const draft = createDraftSession()
      activeSessionRef.current = draft
      setActiveSession(draft)
    }
    if (initRef.current) return
    initRef.current = true

    const requestCwd = config.cwd
    const requestToken = sessionRequestTokenRef.current + 1
    sessionRequestTokenRef.current = requestToken
    setSessionLoading(true)

    const isStillCurrent = () =>
      isCurrentWorkspaceRequest({
        pendingRequestToken: sessionRequestTokenRef.current,
        requestToken,
        activeCwd: cwdRef.current,
        requestCwd,
      })

    void (async () => {
      try {
        const preferredSessionId = loadActiveSession(requestCwd)
        const res = await fetch(
          `/api/sessions?cwd=${encodeURIComponent(requestCwd)}`,
        )
        if (!res.ok) throw new Error('Failed to load sessions')
        const data = (await res.json()) as SessionsResponse
        if (!isStillCurrent()) return

        const savedSessions = data.sessions || []
        setSessions(savedSessions)
        const initialSessionId = resolveInitialSessionId(
          savedSessions,
          preferredSessionId,
        )

        if (initialSessionId) {
          const summary = savedSessions.find(
            (session) => session.id === initialSessionId,
          )
          if (summary) {
            activeSessionRef.current = summary
            setActiveSession(summary)
          }
          await loadSessionRef.current?.(initialSessionId, {
            requestCwd,
            requestToken,
          })
        } else {
          const draft = createDraftSession()
          activeSessionRef.current = draft
          setActiveSession(draft)
          setSessions([draft])
          dispatch({ type: 'set_messages', messages: [] })
          setLoading(false)
        }
      } catch (e) {
        console.error('Failed to initialize sessions:', e)
      } finally {
        if (isStillCurrent()) setSessionLoading(false)
      }
    })()
  }, [config.cwd, stopStreaming])

  useEffect(() => {
    return () => {
      stopStreaming()
    }
  }, [stopStreaming])

  return {
    messages,
    loading,
    sessions,
    activeSession,
    sessionLoading,
    pendingPermission: pendingPermissions[0] ?? null,
    send,
    rewindAndSend,
    cancel,
    decidePermission,
    createSession,
    selectSession,
    deleteSession,
  }
}
