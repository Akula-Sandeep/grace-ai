import { useCallback, useEffect, useRef, useState } from 'react';

function recognitionClass() {
  if (typeof window === 'undefined') return null;
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

const ERROR_MESSAGES = {
  'not-allowed': 'Microphone access is blocked. Allow it in your browser’s site settings to dictate.',
  'service-not-allowed': 'Microphone access is blocked. Allow it in your browser’s site settings to dictate.',
  'audio-capture': 'No microphone was found.',
  'no-speech': 'I didn’t hear anything. Try again when you’re ready.',
  network: 'Voice input needs a network connection.',
};

/**
 * Optional voice dictation (UI spec §30) using the browser's speech
 * recognition. The transcript goes into the composer for correction; nothing
 * is sent automatically.
 */
export function useDictation({ getText, setText }) {
  const supported = Boolean(recognitionClass());
  const [state, setState] = useState(supported ? 'idle' : 'unsupported');
  const [message, setMessage] = useState('');
  const recognitionRef = useRef(null);
  const baseRef = useRef('');

  const stop = useCallback(() => {
    recognitionRef.current?.stop();
  }, []);

  const cancel = useCallback(() => {
    const recognition = recognitionRef.current;
    if (!recognition) return;
    recognition.onresult = null;
    recognition.abort();
    setText(baseRef.current);
    setState('idle');
    setMessage('Dictation cancelled.');
  }, [setText]);

  const start = useCallback(() => {
    const Recognition = recognitionClass();
    if (!Recognition) {
      setState('unsupported');
      return;
    }
    const recognition = new Recognition();
    recognitionRef.current = recognition;
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = navigator.language || 'en-US';
    const base = getText();
    baseRef.current = base;
    const separator = base && !/\s$/.test(base) ? ' ' : '';
    recognition.onresult = (event) => {
      let transcript = '';
      for (let index = 0; index < event.results.length; index += 1) {
        transcript += event.results[index][0].transcript;
      }
      setText(`${base}${separator}${transcript.trim()}`);
    };
    recognition.onerror = (event) => {
      if (event.error === 'aborted') return;
      setState(event.error === 'not-allowed' || event.error === 'service-not-allowed' ? 'denied' : 'error');
      setMessage(ERROR_MESSAGES[event.error] || 'Voice input stopped unexpectedly.');
    };
    recognition.onend = () => {
      recognitionRef.current = null;
      setState((current) => (current === 'listening' ? 'idle' : current));
    };
    setMessage('');
    setState('listening');
    try {
      recognition.start();
    } catch {
      setState('error');
      setMessage('Voice input could not start.');
    }
  }, [getText, setText]);

  useEffect(() => () => recognitionRef.current?.abort(), []);

  return { supported, state, message, start, stop, cancel };
}

/** Read an answer aloud with the browser's speech synthesis; can be interrupted. */
export function useReadAloud() {
  const supported = typeof window !== 'undefined' && 'speechSynthesis' in window && typeof window.SpeechSynthesisUtterance === 'function';
  const [speakingId, setSpeakingId] = useState(null);

  const stop = useCallback(() => {
    if (supported) window.speechSynthesis.cancel();
    setSpeakingId(null);
  }, [supported]);

  const speak = useCallback((id, text) => {
    if (!supported) return;
    window.speechSynthesis.cancel();
    const plain = String(text || '')
      .replace(/```[\s\S]*?```/g, ' ')
      .replace(/[*_#>`[\]()|-]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    const utterance = new window.SpeechSynthesisUtterance(plain);
    utterance.lang = navigator.language || 'en-US';
    utterance.onend = () => setSpeakingId((current) => (current === id ? null : current));
    utterance.onerror = () => setSpeakingId((current) => (current === id ? null : current));
    setSpeakingId(id);
    window.speechSynthesis.speak(utterance);
  }, [supported]);

  useEffect(() => () => { if (supported) window.speechSynthesis.cancel(); }, [supported]);

  return { supported, speakingId, speak, stop };
}
