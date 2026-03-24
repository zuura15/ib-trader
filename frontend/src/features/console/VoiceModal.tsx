import { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';

/** Resolve the SpeechRecognition constructor, or null if unsupported. */
function getSpeechRecognition(): SpeechRecognitionConstructor | null {
  return window.SpeechRecognition ?? window.webkitSpeechRecognition ?? null;
}

/**
 * Check whether the Web Speech API is available in this browser.
 * Used by the parent to decide whether to render the mic button at all.
 */
export function isSpeechAvailable(): boolean {
  return getSpeechRecognition() !== null;
}

/**
 * Full-screen voice input modal.
 *
 * Flow: user taps mic → modal opens → speech recognition starts with
 * live interim transcription → user taps Submit (✓) to send the
 * transcript to the command input, or Cancel to discard.
 *
 * Uses `continuous: true` + `interimResults: true` so the user sees
 * words appearing in real-time and the session stays alive until
 * explicitly stopped.
 */
export function VoiceModal({
  open,
  onClose,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  onSubmit: (transcript: string) => void;
}) {
  const Ctor = getSpeechRecognition();
  const recognitionRef = useRef<SpeechRecognition | null>(null);
  const [transcript, setTranscript] = useState('');
  const [interim, setInterim] = useState('');
  const [listening, setListening] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const stopRecognition = useCallback(() => {
    recognitionRef.current?.stop();
  }, []);

  const startRecognition = useCallback(() => {
    if (!Ctor) return;

    // Clean up any existing instance
    recognitionRef.current?.abort();

    const recognition = new Ctor();
    recognition.lang = 'en-US';
    // Single-utterance mode: stops after a natural pause.
    // `continuous: true` causes Chrome on Android to re-transcribe
    // overlapping audio segments, producing "buybuy qqqbuybuy" duplication.
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;

    recognition.onstart = () => {
      setListening(true);
      setError(null);
    };

    recognition.onresult = (event: SpeechRecognitionEvent) => {
      // With continuous=false, there is only one result entry.
      // It starts as interim (isFinal=false) and becomes final when
      // the engine is confident. We show whatever we have.
      const result = event.results[0];
      if (!result) return;

      const text = result[0].transcript.trim();
      if (result.isFinal) {
        setTranscript(text);
        setInterim('');
      } else {
        setInterim(text);
      }
    };

    recognition.onerror = (event: SpeechRecognitionErrorEvent) => {
      if (event.error === 'aborted') return;
      if (event.error === 'no-speech') {
        setError('No speech detected. Tap the mic to try again.');
      } else if (event.error === 'not-allowed') {
        setError('Microphone access denied. Check browser permissions.');
      } else {
        setError(`Speech error: ${event.error}`);
      }
      setListening(false);
    };

    recognition.onend = () => {
      setListening(false);
      recognitionRef.current = null;
    };

    recognitionRef.current = recognition;

    try {
      recognition.start();
    } catch {
      setListening(false);
      setError('Could not start voice input.');
    }
  }, [Ctor]);

  // Start recognition when modal opens, clean up when it closes
  useEffect(() => {
    if (open) {
      setTranscript('');
      setInterim('');
      setError(null);
      startRecognition();
    } else {
      recognitionRef.current?.abort();
    }
    return () => {
      recognitionRef.current?.abort();
    };
  }, [open, startRecognition]);

  const handleSubmit = () => {
    const text = (transcript || interim).trim();
    if (text) {
      stopRecognition();
      onSubmit(text);
    }
  };

  const handleCancel = () => {
    stopRecognition();
    onClose();
  };

  const handleRetry = () => {
    setTranscript('');
    setInterim('');
    setError(null);
    startRecognition();
  };

  const displayText = transcript || interim;
  const hasText = displayText.trim().length > 0;

  if (!open) return null;

  return createPortal(
    <div
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 10000,
        background: 'rgba(0, 0, 0, 0.92)',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
      }}
      onClick={(e) => {
        // Close on backdrop tap only if no text entered
        if (e.target === e.currentTarget && !hasText) {
          handleCancel();
        }
      }}
    >
      {/* Title */}
      <div style={{ color: 'var(--text-muted)', fontSize: 14, marginBottom: 24, letterSpacing: '0.1em', textTransform: 'uppercase' }}>
        Voice Input
      </div>

      {/* Mic indicator */}
      <div
        style={{
          width: 80,
          height: 80,
          borderRadius: '50%',
          background: listening ? 'var(--accent-red)' : 'var(--bg-secondary)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: 36,
          marginBottom: 32,
          animation: listening ? 'mic-pulse 1.5s ease-in-out infinite' : 'none',
          cursor: 'pointer',
        }}
        onClick={listening ? stopRecognition : handleRetry}
        role="button"
        aria-label={listening ? 'Stop listening' : 'Start listening'}
      >
        &#x1F3A4;
      </div>

      {/* Live transcription */}
      <div
        style={{
          minHeight: 80,
          maxWidth: '100%',
          textAlign: 'center',
          marginBottom: 32,
        }}
      >
        {error ? (
          <div style={{ color: 'var(--accent-red)', fontSize: 16 }}>{error}</div>
        ) : hasText ? (
          <div style={{ color: 'var(--text-primary)', fontSize: 24, fontWeight: 600, fontFamily: 'monospace', lineHeight: 1.4 }}>
            {transcript}
            {interim && !transcript && (
              <span style={{ opacity: 0.5 }}>{interim}</span>
            )}
          </div>
        ) : listening ? (
          <div style={{ color: 'var(--text-muted)', fontSize: 18 }}>
            Listening...
          </div>
        ) : (
          <div style={{ color: 'var(--text-muted)', fontSize: 16 }}>
            Tap the mic to start
          </div>
        )}
      </div>

      {/* Action buttons */}
      <div style={{ display: 'flex', gap: 16, width: '100%', maxWidth: 320 }}>
        <button
          type="button"
          onClick={handleCancel}
          style={{
            flex: 1,
            padding: '16px 0',
            fontSize: 16,
            fontWeight: 600,
            border: '1px solid var(--border-default)',
            borderRadius: 8,
            background: 'var(--bg-secondary)',
            color: 'var(--text-secondary)',
            cursor: 'pointer',
            minHeight: 56,
          }}
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={!hasText}
          style={{
            flex: 1,
            padding: '16px 0',
            fontSize: 16,
            fontWeight: 600,
            border: 'none',
            borderRadius: 8,
            background: hasText ? 'var(--accent-blue)' : 'var(--bg-secondary)',
            color: hasText ? '#fff' : 'var(--text-muted)',
            cursor: hasText ? 'pointer' : 'default',
            minHeight: 56,
            opacity: hasText ? 1 : 0.5,
          }}
        >
          &#x2713; Use
        </button>
      </div>
    </div>,
    document.body,
  );
}
