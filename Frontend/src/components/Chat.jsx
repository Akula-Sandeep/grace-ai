import { lazy, memo, Suspense, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import {
  api,
  apiFetchStream,
  consumeChatStream,
  newRequestId,
  userFacingError,
} from '../lib/api';
import { clearSession } from '../lib/auth';
import {
  abortChatController,
  getSnapshot,
  setChatController,
  subscribe,
  updateChatRuntime,
} from '../lib/chatStore';
import Icon from './Icon';
import StreamingBadge from './StreamingBadge';
import AnswerSources from './AnswerSources';
import FeedbackReasons from './FeedbackReasons';
import FollowUps, { chipList } from './FollowUps';
import ActionNotice from './ActionNotice';
import MemoryNotice from './MemoryNotice';
import StepList, { addStep } from './StepList';
import { ChatGreeting, ChatSuggestions } from './ChatHome';
import { t } from '../lib/i18n';
import { linkCitations } from '../lib/citations';
import { applyTemplate, matchSlashCommands } from '../lib/shortcuts';
import useAttachments, { ATTACHMENT_TYPES } from '../lib/useAttachments';
import { useDictation, useReadAloud } from '../lib/useVoice';

const Markdown = lazy(() => import('./Markdown'));

function referenceKey(reference) {
  return reference.kind === 'person' ? `person-${reference.person_id}` : `movie-${reference.tmdb_id}`;
}

// "[2]" in an answer becomes a link to the second listed source (UI spec §39).

const MessageBubble = memo(function MessageBubble({ role, content, isStreaming, sources }) {
  if (role !== 'assistant') return <p>{content}</p>;
  const linked = linkCitations(content, sources);
  return (
    <Suspense fallback={<p className="streaming-text">{content}</p>}>
      <Markdown content={linked} isStreaming={isStreaming} />
    </Suspense>
  );
});

const MAX_MESSAGE_LENGTH = 12000;

// Why a saved turn failed, for conversations reopened later (the live
// error text isn't stored; its code is). Mirrors the server's failure copy.
const SAVED_FAILURE_COPY = {
  rate_limited: 'Grace’s AI models were busy, so this wasn’t answered. Try again.',
  provider_down: 'The answer service was unavailable, so this wasn’t answered. Try again.',
  timeout: 'This took too long and was stopped before an incomplete answer. Try again.',
  deadline_exceeded: 'This took too long and was stopped before an incomplete answer. Try again.',
  unsafe: 'Grace can’t help with that request as written.',
  no_evidence: 'Grace couldn’t find reliable evidence for that, so it didn’t guess.',
};

// Text shown inside a failed assistant turn. Stream errors carry their own
// user-facing copy (message + next step); HTTP failures are mapped by status.
function chatFailureText(err) {
  if (err.code && !err.status) return [err.message, err.nextStep].filter(Boolean).join(' ');
  return userFacingError({ ...err, code: err.problemCode || err.code });
}

function Chat({
  cid,
  setCid,
  messages,
  setMessages,
  activeMovieTitle,
  setActiveMovieTitle,
  providerData,
  setProviderData,
  setConvos,
  setError,
  isOnline,
  isAuthenticated,
  preferences,
  onLoadOlderMessages,
  pendingPrompt = '',
  onPendingPromptConsumed,
  onStateChange,
  onNavigate,
  onOpenMovie,
  onSaveMovie,
  onOpenPerson,
  savedMovieIds = [],
  projectId = null,
}) {
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const { busy, streaming, currentStatus, activeAssistantId } = useSyncExternalStore(
    subscribe,
    getSnapshot,
    getSnapshot,
  );
  const [currentProviderMovieIds, setCurrentProviderMovieIds] = useState([]);
  const [providerTitles, setProviderTitles] = useState({});
  const [lastFailed, setLastFailed] = useState('');
  const [retryCountdown, setRetryCountdown] = useState(0);
  const sendRef = useRef(null);
  const [pendingFreshRetry, setPendingFreshRetry] = useState(null);
  const [editingMessageId, setEditingMessageId] = useState('');
  const [selectedAnswerByParent, setSelectedAnswerByParent] = useState({});
  const [feedbackFor, setFeedbackFor] = useState(null);
  const [slashIndex, setSlashIndex] = useState(0);
  const [slashClosedFor, setSlashClosedFor] = useState('');
  const [copiedMessageId, setCopiedMessageId] = useState('');
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [finalAnnouncement, setFinalAnnouncement] = useState('');
  const isAtBottomRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  const chatContainerRef = useRef(null);
  const olderMessagesLoadingRef = useRef(false);
  const composerRef = useRef(null);
  const fileInputRef = useRef(null);
  const textRef = useRef('');
  const [dragging, setDragging] = useState(false);
  const attachments = useAttachments();
  const readAloud = useReadAloud();
  const dictation = useDictation({
    getText: () => textRef.current,
    setText: (value) => updateText(value),
  });
  const cidRef = useRef(cid);
  const requestRef = useRef(0);
  const offlineAbortRef = useRef(false);
  const tokenBufferRef = useRef('');
  const tokenFrameRef = useRef(0);
  const activeMessageRef = useRef(null);
  const activeAnswerRef = useRef('');
  const activeTurnRef = useRef(null);
  const visibleMessages = messages.filter((message) => {
    if (message.role !== 'assistant' || message.status === 'error' || !message.parent_id) return true;
    const siblings = messages.filter((candidate) => (
      candidate.role === 'assistant'
      && candidate.parent_id === message.parent_id
      && candidate.status !== 'error'
    ));
    const selected = selectedAnswerByParent[message.parent_id] || siblings.at(-1)?.id;
    return message.id === selected;
  });

  useEffect(() => () => {
    requestRef.current += 1;
    abortChatController();
    // The runtime store outlives this component; never leave it "busy" for
    // the next mount, or the composer would show Stop forever and drop input.
    updateChatRuntime({ busy: false, streaming: false, currentStatus: null, activeAssistantId: '' });
  }, []);


  useEffect(() => {
    if (cidRef.current !== cid && busy && !activeAssistantId && !activeTurnRef.current) {
      requestRef.current += 1;
      abortChatController();
      updateChatRuntime({ busy: false, streaming: false, currentStatus: null });
    }
    cidRef.current = cid;
  }, [activeAssistantId, busy, cid]);

  useEffect(() => {
    // A turn failed because its conversation no longer exists on the server:
    // resend the same message as the first turn of a new conversation, once,
    // after the failed turn has fully finished.
    if (!pendingFreshRetry || busy) return;
    const next = pendingFreshRetry;
    setPendingFreshRetry(null);
    void send(next.text, { clientMessageId: next.clientMessageId, retry: true, freshConversation: true });
    // send is redefined every render; only these two values should trigger this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingFreshRetry, busy]);

  useEffect(() => {
    setText(readDraft(cid || 'new') || '');
  }, [cid]);

  // "Ask Grace" from a movie or person page pre-fills the composer so the
  // user can edit before sending; it is never sent on their behalf.
  useEffect(() => {
    if (!pendingPrompt) return;
    setText(pendingPrompt);
    writeDraft(cidRef.current || 'new', pendingPrompt);
    onPendingPromptConsumed?.();
    window.requestAnimationFrame(() => composerRef.current?.focus());
  }, [pendingPrompt, onPendingPromptConsumed]);

  useEffect(() => {
    const handleOffline = () => {
      setError("You're offline. Reconnecting...");
      if (busy) {
        offlineAbortRef.current = true;
        abortChatController();
      }
    };
    const handleOnline = () => {
      setError((current) => (
        current === "You're offline. Reconnecting..." ? '' : current
      ));
    };

    window.addEventListener('offline', handleOffline);
    window.addEventListener('online', handleOnline);
    return () => {
      window.removeEventListener('offline', handleOffline);
      window.removeEventListener('online', handleOnline);
    };
  }, [busy, setError]);

  useEffect(() => {
    const container = chatContainerRef.current;
    if (!container) return undefined;

    const handleScroll = () => {
      // Only a scroll *up* by the user stops following the answer. A long streamed block
      // (a table, a list) grows the page between our scroll and this event, and reading
      // that as "the user left the bottom" showed "Jump to latest" mid-answer (2026-10-14).
      const movedUp = container.scrollTop < lastScrollTopRef.current - 2;
      lastScrollTopRef.current = container.scrollTop;
      const nearBottom =
        container.scrollHeight - container.scrollTop - container.clientHeight < 40;
      const atBottom = nearBottom || (isAtBottomRef.current && !movedUp);
      isAtBottomRef.current = atBottom;
      setIsAtBottom(atBottom);
      if (
        container.scrollTop < 80
        && onLoadOlderMessages
        && !olderMessagesLoadingRef.current
      ) {
        olderMessagesLoadingRef.current = true;
        const previousHeight = container.scrollHeight;
        const previousTop = container.scrollTop;
        void onLoadOlderMessages().then((loaded) => {
          if (loaded) {
            requestAnimationFrame(() => {
              container.scrollTop = container.scrollHeight - previousHeight + previousTop;
            });
          }
        }).finally(() => {
          olderMessagesLoadingRef.current = false;
        });
      }
    };
    container.addEventListener('scroll', handleScroll, { passive: true });
    return () => container.removeEventListener('scroll', handleScroll);
  }, [onLoadOlderMessages]);

  useEffect(() => {
    // Rotating a phone or resizing the window changes the column height
    // without a scroll event; stay pinned to the latest message if we were.
    const container = chatContainerRef.current;
    if (!container || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(() => {
      if (isAtBottomRef.current) container.scrollTop = container.scrollHeight;
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (retryCountdown <= 0) return undefined;
    const timer = window.setTimeout(() => {
      setRetryCountdown((remaining) => Math.max(0, remaining - 1));
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [retryCountdown]);

  const coolingDown = retryCountdown > 0 && Boolean(lastFailed?.autoRetry);
  useEffect(() => {
    if (retryCountdown > 0 || !lastFailed?.autoRetry || busy || !isOnline) return undefined;
    // Run outside the render that zeroed the countdown.
    const timer = window.setTimeout(() => {
      sendRef.current?.(lastFailed.text, { ...lastFailed, autoRetry: false, autoRetried: true });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [retryCountdown, lastFailed, busy, isOnline]);

  useEffect(() => {
    if (!isAtBottom) return undefined;
    const frame = requestAnimationFrame(() => {
      const container = chatContainerRef.current;
      if (container && isAtBottomRef.current) {
        container.scrollTop = container.scrollHeight;
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [messages, currentStatus, streaming, isAtBottom]);

  

  function updateText(value, focus = false) {
  const nextValue = String(value).slice(0, MAX_MESSAGE_LENGTH);
  textRef.current = nextValue;
  setText(nextValue);
  writeDraft(cidRef.current || 'new', nextValue);

  const element = composerRef.current;

  if (element) {
    element.style.height = 'auto';
    const height = Math.min(element.scrollHeight, 150);
    element.style.height = `${height}px`;
    element.style.overflowY = element.scrollHeight > 150 ? 'auto' : 'hidden';

    if (focus) {
      requestAnimationFrame(() => {
        element.focus();
        const length = element.value.length;
        element.setSelectionRange(length, length);
      });
    }
  }
}

  const slashCommands = slashClosedFor === text ? [] : matchSlashCommands(text, { signedIn: Boolean(isAuthenticated) });
  const activeSlash = slashCommands.length ? Math.min(slashIndex, slashCommands.length - 1) : -1;

  function chooseSlash(item) {
    setSlashIndex(0);
    if (item.route) {
      updateText('');
      onNavigate?.(item.route);
      return;
    }
    const { text: next, cursor } = applyTemplate(item.template);
    updateText(next);
    requestAnimationFrame(() => {
      const element = composerRef.current;
      if (!element) return;
      element.focus();
      element.setSelectionRange(cursor, cursor);
    });
  }

  function cancelSend() {
    const assistantId = activeMessageRef.current;
    requestRef.current += 1;
    abortChatController();
    if (tokenFrameRef.current) {
      cancelAnimationFrame(tokenFrameRef.current);
      tokenFrameRef.current = 0;
      activeAnswerRef.current += tokenBufferRef.current;
      tokenBufferRef.current = '';
    }
    setMessages((current) => current.map((message) => (
      message.id === assistantId
        ? { ...message, content: activeAnswerRef.current, status: 'stopped', streaming: false }
        : message
    )));
    if (activeTurnRef.current) {
      setLastFailed({ ...activeTurnRef.current, retry: false });
    }
    updateChatRuntime({ streaming: false, busy: false, currentStatus: null });
  }

  async function copyMessage(message) {
    try {
      await navigator.clipboard.writeText(message.content || '');
      setCopiedMessageId(message.id || message.content);
      window.setTimeout(() => setCopiedMessageId(''), 1600);
    } catch {
      setError('Copying is unavailable in this browser.');
    }
  }

  async function rateMessage(message, choice) {
    // Pressing the same thumb again clears it; a thumbs down asks what went wrong.
    const feedback = message.feedback === choice ? 'none' : choice;
    try {
      await api(`/messages/${message.id}/feedback?feedback=${feedback}`, { method: 'PATCH' });
      setMessages((current) => current.map((item) => (
        item.id === message.id ? { ...item, feedback: feedback === 'none' ? null : feedback, feedback_reason: null } : item
      )));
      setFeedbackFor(feedback === 'down' ? message.id : null);
    } catch (err) {
      setError(userFacingError(err));
    }
  }

  async function explainFeedback(message, detail) {
    try {
      await api(`/messages/${message.id}/feedback?feedback=down`, { method: 'PATCH', body: JSON.stringify(detail) });
      setMessages((current) => current.map((item) => (
        item.id === message.id ? { ...item, feedback: 'down', feedback_reason: detail.reason } : item
      )));
      return true;
    } catch (err) {
      setError(userFacingError(err));
      return false;
    }
  }

  function editMessage(message) {
    setEditingMessageId(message.id);
    updateText(message.content || '', true);
  }

  function parentUserMessage(message) {
    let parentId = message.parent_id;
    const visited = new Set();
    while (parentId && !visited.has(parentId)) {
      visited.add(parentId);
      const parent = messages.find((item) => item.id === parentId);
      if (!parent) return null;
      if (parent.role === 'user') return parent;
      parentId = parent.parent_id;
    }
    return null;
  }

  useEffect(() => {
    sendRef.current = send;
  });
  // The draft is read from textRef, which updates synchronously on every
  // keystroke. React state can lag one render behind, so text typed or pasted
  // and sent immediately (Enter in the same frame) used to be silently ignored.
  async function send(requestText = textRef.current ?? text, options = {}) {
    const {
      clientMessageId = crypto.randomUUID(),
      retry = false,
      regenerateOf = null,
      continueFrom = null,
      editMessageId = editingMessageId || null,
      freshConversation = false,
    } = options;
    const isNewTurn = !retry && !regenerateOf && !continueFrom;
    const images = isNewTurn ? attachments.ready : [];
    if (isNewTurn && attachments.pending) return;
    const value = requestText.trim() || (images.length ? 'What can you tell me about this image?' : '');
    if (!value || busy || !isOnline) return;
    // While Grace waits out a model rate limit, a new message would only fail
    // the same way; the composer keeps the draft until the wait is over.
    if (coolingDown && !options.autoRetried && !retry) return;
    if (dictation.state === 'listening') dictation.stop();
    if (retry) {
      // Drop the failed attempt for this turn so a successful retry replaces
      // it instead of leaving a stale failure notice above the new answer.
      setMessages((current) => current.filter((message) => !(
        message.role === 'assistant'
        && message.status === 'error'
        && message.parent_id === clientMessageId
      )));
    }

    setText('');
    textRef.current = '';
    if (images.length) attachments.clear();
    setEditingMessageId('');
    writeDraft(cidRef.current || 'new', '');
    if (composerRef.current) {
      composerRef.current.style.height = '';
      composerRef.current.style.overflowY = 'hidden';
    }

    if (!retry && !regenerateOf && !continueFrom) {
      setMessages((current) => (
        current.some((message) => message.id === clientMessageId)
          ? current
          : [
            ...current,
            {
              id: clientMessageId,
              role: 'user',
              content: value,
              images,
              status: 'complete',
              parent_id: editMessageId
                ? current.find((message) => message.id === editMessageId)?.parent_id || null
                : current.at(-1)?.id || null,
            },
          ]
      ));
    }
    isAtBottomRef.current = true;
    setIsAtBottom(true);
    requestAnimationFrame(() => {
      const container = chatContainerRef.current;
      if (container) container.scrollTop = container.scrollHeight;
    });

    updateChatRuntime({ busy: true, streaming: false, currentStatus: null });
    setSending(true);
    setFinalAnnouncement('Grace is answering');
    setCurrentProviderMovieIds([]);
    offlineAbortRef.current = false;
    const requestId = ++requestRef.current;
    const startingConversationId = cidRef.current;
    const provisionalAssistantId = `assistant-${clientMessageId}`;
    let assistantId = provisionalAssistantId;
    activeMessageRef.current = provisionalAssistantId;
    const controller = new AbortController();
    setChatController(controller);
    updateChatRuntime({ activeAssistantId: assistantId });
    activeAnswerRef.current = '';
    activeTurnRef.current = { text: value, clientMessageId };
    setError('');
    setLastFailed('');
    let answer = '';
    let finalConversationId = startingConversationId;
    let receivedDone = false;

    try {
      const streamResponse = await apiFetchStream('/chat/stream', {
        method: 'POST',
        signal: controller.signal,
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          'X-Request-ID': newRequestId(),
        },
        body: JSON.stringify({
          message: continueFrom ? 'Continue the previous answer from where it stopped.' : value,
          conversation_id: freshConversation ? null : startingConversationId,
          client_message_id: clientMessageId,
          parent_id: !freshConversation && !editMessageId && !regenerateOf
            ? messages.at(-1)?.id || null
            : null,
          edit_message_id: editMessageId,
          regenerate_of: regenerateOf,
          continue_from: continueFrom,
          timezone: browserTimezone(),
          ...(images.length ? { images } : {}),
          // A new chat opened from a project starts inside it.
          ...((freshConversation || !startingConversationId) && projectId != null ? { project_id: projectId } : {}),
        }),
      });

      const updateAnswer = (payload) => {
        if (payload.event === 'started') {
          finalConversationId = payload.conversation_id;
          // The chat exists from here on, with the message kept, even if this turn then fails:
          // list it now (it used to appear only after a finished answer).
          if (setConvos && payload.conversation_id != null) {
            setConvos((current) => (
              current.some((conversation) => String(conversation.id) === String(payload.conversation_id))
                ? current
                : [{
                  id: payload.conversation_id,
                  title: value.split(/\s+/).slice(0, 6).join(' '),
                  updated_at: new Date().toISOString(),
                  ...(projectId != null ? { project_id: projectId } : {}),
                }, ...current]
            ));
          }
          assistantId = payload.message_id;
          activeMessageRef.current = assistantId;
          updateChatRuntime({ activeAssistantId: assistantId });
          setCid(payload.conversation_id);
          cidRef.current = payload.conversation_id;
          setMessages((current) => {
            const provisional = current.find((message) => message.id === provisionalAssistantId);
            if (provisional) {
              return current.map((message) => (
                message.id === provisionalAssistantId
                  ? { ...message, id: assistantId }
                  : message
              ));
            }
            if (current.some((message) => message.id === assistantId)) return current;
            return [
              ...current,
              {
                id: assistantId,
                parent_id: regenerateOf || continueFrom || clientMessageId,
                role: 'assistant',
                content: '',
                status: 'streaming',
                streaming: true,
              },
            ];
          });
          return;
        }
        if (payload.event === 'error') {
          const streamError = new Error(payload.message);
          streamError.code = payload.code;
          streamError.retryable = payload.retryable;
          streamError.nextStep = payload.next_step;
          streamError.retryAfterSeconds = payload.retry_after_seconds;
          throw streamError;
        }
        if (payload.event === 'status') {
          updateChatRuntime({ currentStatus: payload.label });
          setMessages((current) => current.map((message) => (
            message.id === assistantId || message.id === activeMessageRef.current
              ? { ...message, steps: addStep(message.steps, payload.label) }
              : message
          )));
          return;
        }
        if (payload.event === 'providers') {
          const providerPayload = payload.providers;
          if (providerPayload.movie_id != null) {
            setProviderData((current) => ({
              ...current,
              [providerPayload.movie_id]: providerPayload.providers,
            }));
            setProviderTitles((current) => ({
              ...current,
              [providerPayload.movie_id]: providerPayload.title || `Movie ${providerPayload.movie_id}`,
            }));
            setCurrentProviderMovieIds((current) => (
              current.includes(providerPayload.movie_id)
                ? current
                : [...current, providerPayload.movie_id]
            ));
            if (providerPayload.title) setActiveMovieTitle(providerPayload.title);
          }
          return;
        }
        if (payload.event === 'state') {
          // A chat action changed the user's lists: refresh the watchlist,
          // watched list and "saved" markers right away, not on next reload.
          onStateChange?.(payload);
          return;
        }
        if (payload.event === 'action') {
          // A verified list change: shown as a card with its Undo under this reply.
          setMessages((current) => current.map((message) => (
            message.id === assistantId
              ? { ...message, actions: [...(message.actions || []), payload.text] }
              : message
          )));
          return;
        }
        if (payload.event === 'memory') {
          const learned = payload.items.filter((item) => item && Number.isInteger(item.id) && typeof item.label === 'string');
          if (learned.length) {
            setMessages((current) => current.map((message) => (
              message.id === assistantId ? { ...message, memory: learned } : message
            )));
          }
          return;
        }
        if (payload.event === 'references') {
          const references = Array.isArray(payload.references) ? payload.references : [];
          setMessages((current) => current.map((message) => (
            message.id === assistantId
              ? { ...message, references: [...(message.references || []), ...references].filter((item, index, all) => all.findIndex((other) => referenceKey(other) === referenceKey(item)) === index) }
              : message
          )));
          return;
        }
        if (payload.event === 'sources') {
          const answerSources = Array.isArray(payload.sources) ? payload.sources : [];
          const answerAssessment = payload.assessment && typeof payload.assessment === 'object' ? payload.assessment : null;
          setMessages((current) => current.map((message) => (
            message.id === assistantId ? { ...message, sources: answerSources, assessment: answerAssessment } : message
          )));
          return;
        }
        if (payload.event === 'delta' || payload.event === 'token') {
          tokenBufferRef.current += payload.text;
          if (!tokenFrameRef.current) {
            tokenFrameRef.current = requestAnimationFrame(() => {
              const fragment = tokenBufferRef.current;
              tokenBufferRef.current = '';
              tokenFrameRef.current = 0;
              answer += fragment;
              activeAnswerRef.current = answer;
              updateChatRuntime({ currentStatus: null, streaming: true });
              setMessages((current) => current.map((message) => (
                message.id === assistantId
                  ? { ...message, content: answer, status: 'streaming', streaming: true }
                  : message
              )));
            });
          }
          return;
        }
        if (payload.event === 'done') {
          receivedDone = true;
          if (tokenFrameRef.current) {
            cancelAnimationFrame(tokenFrameRef.current);
            tokenFrameRef.current = 0;
            answer += tokenBufferRef.current;
            tokenBufferRef.current = '';
            activeAnswerRef.current = answer;
          }
          assistantId = payload.message_id;
          setMessages((current) => current.map((message) => (
            message.id === activeMessageRef.current || message.id === provisionalAssistantId
              ? {
                ...message,
                id: assistantId,
                content: answer,
                status: 'complete',
                streaming: false,
                options: chipList(payload.options),
                suggestions: chipList(payload.suggestions),
              }
              : message
          )));
          setFinalAnnouncement(answer);
          setLastFailed('');
          if (finalConversationId != null && setConvos) {
            const fallback = value.split(/\s+/).slice(0, 6).join(' ');
            setConvos((current) => {
              const existing = current.find((conversation) => String(conversation.id) === String(finalConversationId));
              if (existing) {
                return current.map((conversation) => (
                  String(conversation.id) === String(finalConversationId)
                    ? { ...conversation, title: existing.title === 'New movie conversation' ? fallback : existing.title }
                    : conversation
                ));
              }
              return [{
                id: finalConversationId,
                title: fallback,
                updated_at: new Date().toISOString(),
              }, ...current];
            });
          }
        }
      };

      if (!streamResponse.ok) {
        let detail = '';
        let code = streamResponse.headers.get('X-Problem-Code') || '';
        let requestId = streamResponse.headers.get('X-Request-ID') || '';
        const retryAfterSeconds = Number(streamResponse.headers.get('Retry-After')) || 0;
        try {
          const problem = await streamResponse.json();
          detail = problem?.detail || '';
          code = problem?.code || code;
          requestId = problem?.request_id || requestId;
        } catch {
          // The status is sufficient for a user-facing error.
        }
        const streamError = new Error(detail || `Request failed (${streamResponse.status})`);
        streamError.status = streamResponse.status;
        streamError.problemCode = code;
        streamError.requestId = requestId;
        streamError.retryAfterSeconds = retryAfterSeconds;
        streamError.networkIssue = streamResponse.status >= 500;
        if (streamResponse.status === 401) {
          clearSession();
        }
        throw streamError;
      } else {
        const streamCompleted = await consumeChatStream(
          streamResponse,
          updateAnswer,
          controller.signal,
        );
        if (!streamCompleted) {
          const interrupted = new Error('The streaming response ended before completion.');
          interrupted.networkIssue = true;
          throw interrupted;
        }
      }

      updateChatRuntime({ currentStatus: null });
      if (!receivedDone) throw new Error('The chat stream did not finish.');
    } catch (err) {
      if (err.name === 'AbortError') {
        if (tokenFrameRef.current) {
          cancelAnimationFrame(tokenFrameRef.current);
          tokenFrameRef.current = 0;
          answer += tokenBufferRef.current;
          tokenBufferRef.current = '';
          activeAnswerRef.current = answer;
        }
        if (offlineAbortRef.current && requestId === requestRef.current) {
          offlineAbortRef.current = false;
          setLastFailed({ text: value, clientMessageId, retry: true });
          setMessages((current) => current.map((message) => (
            message.id === activeMessageRef.current
              ? { ...message, content: activeAnswerRef.current, status: 'stopped', streaming: false }
              : message
          )));
        }
        return;
      }
      updateChatRuntime({ currentStatus: null });
      const conversationGone = err.status === 404 && startingConversationId != null && !freshConversation;
      const failureText = conversationGone
        ? 'This conversation is no longer available on the server, so Grace is continuing in a new one.'
        : chatFailureText(err);
      if (conversationGone) {
        // Guest conversations expire, and a deleted conversation can't take
        // new turns. Instead of a dead end where Retry fails forever, start a
        // fresh conversation with the same message once the current turn ends.
        cidRef.current = null;
        setCid(null);
        setPendingFreshRetry({ text: value, clientMessageId });
      }
      const waitSeconds = conversationGone ? 0 : Math.min(120, Math.max(0, Math.round(Number(err.retryAfterSeconds) || 0)));
      const rateLimited = !conversationGone && (
        err.status === 429 || ['rate_limited', 'RATE_LIMITED'].includes(err.code || err.problemCode)
      );
      // Only a rate limit imposes a wait; any other failure can be retried at once.
      setRetryCountdown(rateLimited ? waitSeconds : 0);
      // A busy-model failure is retried automatically once the server's wait
      // is over, the way hosted chat apps do; other failures wait for the user.
      setLastFailed(conversationGone ? '' : {
        text: value,
        clientMessageId,
        retry: true,
        autoRetry: rateLimited && waitSeconds > 0 && !options.autoRetried,
      });
      setMessages((current) => {
        const exists = current.some((message) => (
          message.id === activeMessageRef.current || message.id === provisionalAssistantId
        ));
        if (!exists) {
          return [
            ...current,
            {
              id: provisionalAssistantId,
              parent_id: regenerateOf || continueFrom || clientMessageId,
              role: 'assistant',
              content: activeAnswerRef.current || answer || '',
              status: activeAnswerRef.current || answer ? 'incomplete' : 'error',
              error_code: err.code || err.problemCode || 'STREAM_FAILED',
              error_text: failureText,
              incomplete: Boolean(activeAnswerRef.current || answer),
            },
          ];
        }
        return current.map((message) => {
          if (message.id !== activeMessageRef.current && message.id !== provisionalAssistantId) return message;
          const partial = activeAnswerRef.current || answer || message.content || '';
          return {
            ...message,
            content: partial,
            status: partial ? 'incomplete' : 'error',
            streaming: false,
            error_code: err.code || err.problemCode || 'STREAM_FAILED',
            error_text: failureText,
            incomplete: Boolean(partial),
          };
        });
      });
    } finally {
      if (requestId === requestRef.current) {
        setSending(false);
        setChatController(null);
        updateChatRuntime({
          streaming: false,
          currentStatus: null,
          activeAssistantId: '',
          busy: false,
        });
      }
    }
  }

  const isHome = messages.length === 0;

  return (
    <div className={`chat-shell${isHome ? ' is-home' : ''}`}>
      {!isOnline && (
        <div className="offline-banner" role="status" aria-live="polite">
          You&apos;re offline — we&apos;ll reconnect automatically.
        </div>
      )}
      <section
        ref={chatContainerRef}
        className="messages"
        aria-label="Conversation"
      >
        {isHome && <ChatGreeting isAuthenticated={Boolean(isAuthenticated)} preferences={preferences} />}

        {visibleMessages.map((message, index) => {
          const isConsecutive = index > 0 && visibleMessages[index - 1].role === message.role;
          const answerSiblings = message.role === 'assistant' && message.parent_id
            ? messages.filter((candidate) => (
              candidate.role === 'assistant'
              && candidate.parent_id === message.parent_id
              && candidate.status !== 'error'
            ))
            : [];
          const siblingIndex = answerSiblings.findIndex((candidate) => candidate.id === message.id);
          return (
            <article
              key={message.id}
              className={`message-row ${message.role}${isConsecutive ? ' consecutive' : ''}`}
            >
            {message.id === activeAssistantId && currentStatus && !message.steps?.length && (
              <div className="stream-status-chip">
                <Icon name="bolt" size={14} />
                {currentStatus}
              </div>
            )}
            {message.role === 'assistant' && (
              <StepList steps={message.steps} working={message.id === activeAssistantId && Boolean(currentStatus) && !message.content} />
            )}

            {message.role === 'assistant' &&
              (message.id === activeAssistantId || index === messages.length - 1) &&
              currentProviderMovieIds.length > 0 && (
                <div className="message-streaming-providers" aria-label="Streaming availability">
                  {currentProviderMovieIds.map((movieId) => (
                    <div className="message-provider-item" key={movieId}>
                      <span>{providerTitles[movieId]}</span>
                      <StreamingBadge providers={providerData?.[movieId]} />
                    </div>
                  ))}
                </div>
              )}

            <div className={`message-body ${message.role}`}>
              {message.role === 'assistant' && (
                <span className="message-avatar" aria-hidden="true">
                  <Icon name="spark" size={17} strokeWidth={1.7} />
                </span>
              )}
              <div className="message-content">
                <div
                  className="bubble"
                >
                  {message.images?.length > 0 && (
                    <div className="message-images">
                      {message.images.map((src, imageIndex) => (
                        <img key={imageIndex} src={src} alt={`Attachment ${imageIndex + 1}`} />
                      ))}
                    </div>
                  )}
                  <MessageBubble
                    role={message.role}
                    content={message.content}
                    isStreaming={message.status === 'streaming'}
                    sources={message.sources}
                  />
                </div>
                {message.role === 'assistant' && message.actions?.length > 0 && (
                  <ActionNotice items={message.actions} />
                )}

                {message.role === 'assistant' && message.references?.length > 0 && onOpenMovie && (
                  <div className="answer-movies" role="group" aria-label="Mentioned in this answer">
                    {message.references.slice(0, 8).map((reference) => {
                      if (reference.kind === 'person') {
                        return onOpenPerson ? (
                          <button
                            key={referenceKey(reference)}
                            type="button"
                            className="answer-movie-link"
                            onClick={() => onOpenPerson(reference.person_id)}
                          >
                            <Icon name="user" size={14} />
                            {reference.title}
                          </button>
                        ) : null;
                      }
                      const saved = savedMovieIds.includes(String(reference.tmdb_id));
                      return (
                        <span key={referenceKey(reference)} className="answer-movie">
                          <button
                            type="button"
                            className="answer-movie-link"
                            onClick={() => onOpenMovie(reference.tmdb_id)}
                          >
                            <Icon name="film" size={14} />
                            {reference.title}
                          </button>
                          {onSaveMovie && (
                            <button
                              type="button"
                              className="answer-movie-save"
                              aria-pressed={saved}
                              aria-label={saved ? `Remove ${reference.title} from watchlist` : `Save ${reference.title} to watchlist`}
                              onClick={() => onSaveMovie(reference.tmdb_id, saved)}
                            >
                              <Icon name={saved ? 'check' : 'plus'} size={14} />
                            </button>
                          )}
                        </span>
                      );
                    })}
                  </div>
                )}

                {message.role === 'assistant' && <AnswerSources sources={message.sources} assessment={message.assessment} />}

                {message.role !== 'system' && (
                  <div className="message-actions" aria-label={`${message.role} message actions`}>
                    <button
                      type="button"
                      onClick={() => copyMessage(message)}
                      aria-label="Copy message"
                      title={copiedMessageId === (message.id || message.content) ? t('message.copied') : t('message.copy')}
                    >
                      <Icon name={copiedMessageId === (message.id || message.content) ? 'check' : 'copy'} size={15} />
                      <span className="sr-only">{copiedMessageId === (message.id || message.content) ? t('message.copied') : t('message.copy')}</span>
                    </button>
                    {message.role === 'assistant' && readAloud.supported && message.status !== 'streaming' && message.content && (
                      readAloud.speakingId === message.id ? (
                        <button type="button" onClick={readAloud.stop} aria-label="Stop reading aloud" title="Stop reading">
                          <Icon name="speaker" size={15} />
                          <span className="sr-only">Stop</span>
                        </button>
                      ) : (
                        <button type="button" onClick={() => readAloud.speak(message.id, message.content)} aria-label="Read aloud" title="Read aloud">
                          <Icon name="speaker" size={15} />
                          <span className="sr-only">{t('message.listen')}</span>
                        </button>
                      )
                    )}
                    {message.role === 'user' && (
                      <button type="button" onClick={() => editMessage(message)} aria-label="Edit message" title="Edit">
                        <Icon name="edit" size={15} />
                        <span className="sr-only">{t('message.edit')}</span>
                      </button>
                    )}
                    {message.role === 'assistant' && message.status === 'complete' && (
                      <button
                        type="button"
                        aria-label="Regenerate answer"
                        title={t('message.regenerate')}
                        onClick={() => {
                          const userMessage = parentUserMessage(message);
                          if (userMessage) {
                            send(userMessage.content, { regenerateOf: userMessage.id });
                          }
                        }}
                      >
                        <Icon name="refresh" size={15} />
                      </button>
                    )}
                    {isAuthenticated && message.role === 'assistant' && message.status === 'complete' && (
                      <>
                        <button
                          type="button"
                          onClick={() => rateMessage(message, 'up')}
                          aria-pressed={message.feedback === 'up'}
                          aria-label="Helpful"
                          title="Helpful"
                        >
                          <Icon name="thumbsUp" size={15} />
                        </button>
                        <button
                          type="button"
                          onClick={() => rateMessage(message, 'down')}
                          aria-pressed={message.feedback === 'down'}
                          aria-label="Not helpful"
                          title="Not helpful"
                        >
                          <Icon name="thumbsDown" size={15} />
                        </button>
                      </>
                    )}
                    {answerSiblings.length > 1 && message.status !== 'streaming' && (
                      <div className="answer-switcher" aria-label="Alternative answers">
                        <button
                          type="button"
                          aria-label="Previous answer"
                          onClick={() => {
                            const previous = (siblingIndex - 1 + answerSiblings.length) % answerSiblings.length;
                            setSelectedAnswerByParent((current) => ({
                              ...current,
                              [message.parent_id]: answerSiblings[previous].id,
                            }));
                          }}
                        >
                          ‹
                        </button>
                        <span>{siblingIndex + 1} / {answerSiblings.length}</span>
                        <button
                          type="button"
                          aria-label="Next answer"
                          onClick={() => {
                            const next = (siblingIndex + 1) % answerSiblings.length;
                            setSelectedAnswerByParent((current) => ({
                              ...current,
                              [message.parent_id]: answerSiblings[next].id,
                            }));
                          }}
                        >
                          ›
                        </button>
                      </div>
                    )}
                  </div>
                )}
                {feedbackFor === message.id && message.feedback === 'down' && (
                  <FeedbackReasons
                    onSubmit={(detail) => explainFeedback(message, detail)}
                    onDismiss={() => setFeedbackFor(null)}
                  />
                )}
                {message.role === 'assistant' && message.memory?.length > 0 && (
                  <MemoryNotice items={message.memory} />
                )}
                {message.role === 'assistant' && message.status === 'complete' && index === visibleMessages.length - 1 && (
                  <FollowUps
                    options={message.options}
                    suggestions={message.suggestions}
                    disabled={busy || !isOnline || coolingDown}
                    onPick={(prompt) => send(prompt)}
                  />
                )}
              </div>
            </div>

            {message.role === 'assistant' && message.status === 'stopped' && (
              <div className="message-actions">
                <span>Stopped</span>
                <button
                  type="button"
                  onClick={() => {
                    const userMessage = parentUserMessage(message);
                    if (userMessage) {
                      send(userMessage.content, { continueFrom: message.id });
                    }
                  }}
                >
                  Continue
                </button>
                <button
                  type="button"
                  onClick={() => {
                    const userMessage = parentUserMessage(message);
                    if (userMessage) {
                      send(userMessage.content, { regenerateOf: userMessage.id });
                    }
                  }}
                >
                  {t('message.regenerate')}
                </button>
              </div>
            )}

            {message.role === 'assistant' && (message.status === 'error' || message.status === 'incomplete') && (
              <div className="message-failure" role="alert">
                <Icon name="alert" size={16} />
                <p>
                  {message.error_text
                    || SAVED_FAILURE_COPY[message.error_code]
                    || (message.status === 'incomplete'
                      ? 'This answer was interrupted before it finished.'
                      : 'Grace couldn’t answer this message. Try again.')}
                </p>
                {index === visibleMessages.length - 1 && lastFailed && (coolingDown ? (
                  <span className="message-failure-actions">
                    <span className="message-failure-countdown" aria-live="polite">Retrying in {retryCountdown}s</span>
                    <button
                      type="button"
                      className="retry-button retry-button-quiet"
                      onClick={() => {
                        setLastFailed({ ...lastFailed, autoRetry: false });
                        setRetryCountdown(0);
                      }}
                    >
                      Cancel
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="retry-button"
                    disabled={busy || !isOnline}
                    onClick={() => send(lastFailed.text, { ...lastFailed, autoRetry: false })}
                  >
                    Try again
                  </button>
                ))}
              </div>
            )}
            </article>
          );
        })}

        {busy && !messages.some((message) => message.id === activeAssistantId) && (
          <div
            className="message-row assistant"
            aria-label="Grace is working"
          >
            {currentProviderMovieIds.map((movieId) => (
              <div className="message-provider-item" key={movieId}>
                <span>{providerTitles[movieId]}</span>
                <StreamingBadge providers={providerData?.[movieId]} />
              </div>
            ))}
            <div className="message-body assistant">
              <span className="message-avatar" aria-hidden="true">
                <Icon name="spark" size={17} strokeWidth={1.7} />
              </span>
              <div className="bubble loading-bubble">
                <span className="vx-thinking-text">{currentStatus || (streaming ? 'Writing' : 'Thinking')}</span>
                <span className="dots" aria-hidden="true">
                  <i />
                  <i />
                  <i />
                </span>
              </div>
            </div>
          </div>
        )}

      </section>

      {!isAtBottom && messages.length > 0 && (
        <button
          type="button"
          className="jump-to-latest"
          onClick={() => {
            const container = chatContainerRef.current;
            if (container) container.scrollTop = container.scrollHeight;
            isAtBottomRef.current = true;
            setIsAtBottom(true);
          }}
        >
          Jump to latest
        </button>
      )}
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {busy || sending ? 'Grace is answering' : finalAnnouncement}
      </div>

      {activeMovieTitle && (
        <div className="intelligence-chips" aria-label={`Explore ${activeMovieTitle}`}>
          {[
            { label: 'Soundtrack', icon: 'music', prompt: `soundtrack and songs for ${activeMovieTitle}` },
            { label: 'Visuals', icon: 'film', prompt: `visual and cinematography analysis of ${activeMovieTitle}` },
            { label: 'Awards', icon: 'award', prompt: `awards won by ${activeMovieTitle}` },
            { label: 'Locations', icon: 'pin', prompt: `filming locations and setting of ${activeMovieTitle}` },
            { label: 'Facts', icon: 'lightbulb', prompt: `production facts about ${activeMovieTitle}` },
          ].map((action) => (
            <button
              key={action.label}
              type="button"
              className="intelligence-chip"
              disabled={busy || !isOnline}
              onClick={() => send(action.prompt)}
            >
              <Icon name={action.icon} size={15} />
              {action.label}
            </button>
          ))}
        </div>
      )}
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          send();
        }}
      >
        <div
          className={`composer-box${dragging ? ' dragging' : ''}`}
          onDragOver={(event) => {
            if (Array.from(event.dataTransfer?.types || []).includes('Files')) {
              event.preventDefault();
              setDragging(true);
            }
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            if (!event.dataTransfer?.files?.length) return;
            event.preventDefault();
            setDragging(false);
            attachments.add(event.dataTransfer.files);
          }}
        >
          {slashCommands.length > 0 && (
            <ul className="composer-commands" id="composer-commands" role="listbox" aria-label="Commands">
              {slashCommands.map((item, index) => (
                <li
                  key={item.command}
                  id={`composer-command-${index}`}
                  role="option"
                  aria-selected={index === activeSlash}
                  className={index === activeSlash ? 'is-active' : ''}
                  onMouseDown={(event) => {
                    event.preventDefault(); // keep the focus in the message box
                    chooseSlash(item);
                  }}
                  onMouseEnter={() => setSlashIndex(index)}
                >
                  <code>{item.command}</code>
                  <span>{item.label}</span>
                </li>
              ))}
            </ul>
          )}
          {attachments.items.length > 0 && (
            <ul className="attachment-previews" aria-label="Attached images">
              {attachments.items.map((item) => (
                <li key={item.id} className={`attachment ${item.status}`}>
                  {item.dataUrl ? <img src={item.dataUrl} alt="" /> : <span className="attachment-placeholder" aria-hidden="true" />}
                  <span className="attachment-name">{item.name}</span>
                  {item.status === 'reading' && (
                    <progress max="100" value={item.progress} aria-label={`Preparing ${item.name}`} />
                  )}
                  {item.status === 'error' && (
                    <>
                      <span className="attachment-error" role="alert">{item.error}</span>
                      <button type="button" className="link-button" onClick={() => attachments.retry(item.id)}>Retry</button>
                    </>
                  )}
                  <button type="button" className="attachment-remove" onClick={() => attachments.remove(item.id)} aria-label={item.status === 'reading' ? `Cancel ${item.name}` : `Remove ${item.name}`}>
                    <Icon name="close" size={14} />
                  </button>
                </li>
              ))}
            </ul>
          )}
          {attachments.notice && <p className="attachment-notice" role="alert">{attachments.notice}</p>}
          {dictation.message && <p className="attachment-notice" role="status">{dictation.message}</p>}
          <input
            ref={fileInputRef}
            type="file"
            accept={ATTACHMENT_TYPES.join(',')}
            multiple
            hidden
            onChange={(event) => {
              attachments.add(event.target.files);
              event.target.value = '';
            }}
          />
          <button
            type="button"
            className="composer-tool"
            onClick={() => fileInputRef.current?.click()}
            disabled={busy}
            aria-label="Attach images"
            title="Attach images (PNG, JPEG, WebP or GIF, up to 4 MB)"
          >
            <Icon name="paperclip" size={18} />
          </button>
          <button
            type="button"
            className={`composer-tool${dictation.state === 'listening' ? ' listening' : ''}`}
            onClick={() => (dictation.state === 'listening' ? dictation.stop() : dictation.start())}
            disabled={!dictation.supported || busy}
            aria-pressed={dictation.state === 'listening'}
            aria-label={
              !dictation.supported
                ? 'Voice input isn’t available in this browser'
                : dictation.state === 'listening' ? 'Stop dictation' : 'Dictate a message'
            }
            title={!dictation.supported ? 'Voice input isn’t available in this browser' : dictation.state === 'listening' ? 'Stop dictation (Esc cancels)' : 'Dictate'}
          >
            <Icon name="mic" size={18} />
          </button>
          {dictation.state === 'listening' && <span className="sr-only" role="status">Listening…</span>}
          <textarea
            ref={composerRef}
            value={text}
            onChange={(event) => {
              setSlashIndex(0);
              updateText(event.target.value);
            }}
            onPaste={(event) => {
              const files = Array.from(event.clipboardData?.files || []).filter((file) => file.type.startsWith('image/'));
              if (files.length) {
                event.preventDefault();
                attachments.add(files);
              }
            }}
            aria-controls={slashCommands.length ? 'composer-commands' : undefined}
            aria-activedescendant={activeSlash >= 0 ? `composer-command-${activeSlash}` : undefined}
            onKeyDown={(event) => {
              if (slashCommands.length && !event.nativeEvent.isComposing) {
                if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                  event.preventDefault();
                  const step = event.key === 'ArrowDown' ? 1 : -1;
                  setSlashIndex((activeSlash + step + slashCommands.length) % slashCommands.length);
                  return;
                }
                if ((event.key === 'Enter' && !event.shiftKey) || event.key === 'Tab') {
                  event.preventDefault();
                  chooseSlash(slashCommands[activeSlash]);
                  return;
                }
                if (event.key === 'Escape') {
                  event.preventDefault();
                  setSlashClosedFor(text);
                  return;
                }
              }
              if (event.key === 'Escape' && dictation.state === 'listening') {
                event.preventDefault();
                dictation.cancel();
                return;
              }
              if (
                event.key === 'Enter'
                && !event.shiftKey
                && !event.nativeEvent.isComposing
                && !event.isComposing
              ) {
                event.preventDefault();
                send();
              }
            }}
            placeholder={coolingDown
              ? `Grace is busy. Your message will be retried in ${retryCountdown}s…`
              : isHome ? 'What are you in the mood for?' : t('composer.placeholder')}
            aria-label="Message Grace"
            rows="1"
            maxLength={MAX_MESSAGE_LENGTH}
          />
          {text.length >= MAX_MESSAGE_LENGTH - 500 && (
            <small className="composer-counter" aria-live="polite">{MAX_MESSAGE_LENGTH - text.length} characters remaining</small>
          )}

          {busy ? (
            <button className="send-button stop-button" type="button" onClick={cancelSend} aria-label="Stop generating" title="Stop generating">
              <span aria-hidden="true">■</span>
            </button>
          ) : (
            <button
              className="send-button"
              type="submit"
              disabled={(!text.trim() && !attachments.ready.length) || attachments.pending || !isOnline || coolingDown}
              aria-label={coolingDown ? `Send available in ${retryCountdown} seconds` : 'Send message'}
              title={coolingDown ? `Grace is retrying your last message in ${retryCountdown}s` : undefined}
            >
              <Icon name="arrow" size={20} strokeWidth={2.1} />
            </button>
          )}
        </div>
      </form>
      {isHome && <ChatSuggestions preferences={preferences} disabled={busy || !isOnline} onPrompt={send} />}
    </div>
  );
  
 
}

/* ---------------------------------------------------------
   Movie detail / intelligence
   --------------------------------------------------------- */

function browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    return null;
  }
}

// Drafts survive navigation, refresh and closing the app (UI spec §31).
function readDraft(key) {
  try {
    return localStorage.getItem(`grace_draft_${key}`) || '';
  } catch {
    return '';
  }
}

function writeDraft(key, value) {
  try {
    if (value) localStorage.setItem(`grace_draft_${key}`, value);
    else localStorage.removeItem(`grace_draft_${key}`);
  } catch {
    // Storage unavailable (private mode); the draft lives only in memory.
  }
}

export default Chat;
