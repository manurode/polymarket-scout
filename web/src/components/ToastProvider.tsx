import { createContext, useContext, useState, useCallback, type ReactNode } from 'react';
import type { Severity } from '../types';

// ── Types ────────────────────────────────────────────────────────────

export interface Toast {
  id: string;
  severity: Severity;
  title: string;
  message: string;
  duration: number; // ms, 0 = requires manual dismiss
  sound?: 'click' | 'bell' | 'loss' | 'alarm' | 'siren' | 'pol';
}

interface ToastContextType {
  toasts: Toast[];
  addToast: (toast: Omit<Toast, 'id'>) => void;
  dismissToast: (id: string) => void;
}

// ── Context ──────────────────────────────────────────────────────────

const ToastContext = createContext<ToastContextType | null>(null);

export function useToasts() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToasts must be used within ToastProvider');
  return ctx;
}

// ── Provider ─────────────────────────────────────────────────────────

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const addToast = useCallback((t: Omit<Toast, 'id'>) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    const toast: Toast = { ...t, id };
    setToasts(prev => [...prev.slice(-4), toast]); // Max 5 visible

    // Auto-dismiss
    if (t.duration > 0) {
      setTimeout(() => {
        setToasts(prev => prev.filter(td => td.id !== id));
      }, t.duration);
    }

    // Play sound
    if (t.sound) {
      import('../lib/soundEngine').then(({ soundEngine }) => {
        soundEngine.play(t.sound!);
      });
    }
  }, []);

  const dismissToast = useCallback((id: string) => {
    setToasts(prev => prev.filter(t => t.id !== id));
  }, []);

  return (
    <ToastContext.Provider value={{ toasts, addToast, dismissToast }}>
      {children}
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
    </ToastContext.Provider>
  );
}

// ── Toast Container ──────────────────────────────────────────────────

const severityStyles: Record<Severity, { bg: string; border: string; text: string; icon: string }> = {
  critical: { bg: 'bg-loss/20', border: 'border-loss/60', text: 'text-loss-bright', icon: '🔴' },
  error: { bg: 'bg-orange-500/10', border: 'border-orange-500/50', text: 'text-orange-400', icon: '🟠' },
  warning: { bg: 'bg-warning/10', border: 'border-warning/50', text: 'text-warning', icon: '🟡' },
  info: { bg: 'bg-info/10', border: 'border-info/50', text: 'text-info', icon: '🔵' },
};

function ToastContainer({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: string) => void }) {
  return (
    <div className="fixed top-14 right-4 z-50 flex flex-col gap-2 max-w-sm pointer-events-none">
      {toasts.map(toast => {
        const style = severityStyles[toast.severity];
        return (
          <div
            key={toast.id}
            className={`
              pointer-events-auto
              ${style.bg} ${style.border} border rounded-lg p-3
              shadow-lg shadow-black/40 backdrop-blur-sm
              animate-[slideIn_0.3s_ease-out]
              ${toast.severity === 'critical' ? 'animate-pulse-red' : ''}
            `}
          >
            <div className="flex items-start gap-2">
              <span className="text-sm flex-shrink-0">{style.icon}</span>
              <div className="flex-1 min-w-0">
                <div className={`text-xs font-semibold ${style.text}`}>
                  {toast.title}
                </div>
                <div className="text-[11px] text-text-secondary mt-0.5 leading-tight">
                  {toast.message}
                </div>
              </div>
              <button
                onClick={() => onDismiss(toast.id)}
                className="flex-shrink-0 text-text-tertiary hover:text-text-primary text-xs leading-none mt-0.5"
              >
                ✕
              </button>
            </div>
            {/* Auto-dismiss progress bar */}
            {toast.duration > 0 && (
              <div className="mt-2 bg-bg-primary/50 rounded-full h-0.5 overflow-hidden">
                <div
                  className={`h-full rounded-full ${toast.severity === 'critical' ? 'bg-loss' : 'bg-bg-active'}`}
                  style={{ animation: `shrink ${toast.duration}ms linear forwards` }}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// Add animations to global CSS
const style = document.createElement('style');
style.textContent = `
  @keyframes slideIn {
    from { opacity: 0; transform: translateX(100%); }
    to { opacity: 1; transform: translateX(0); }
  }
  @keyframes shrink {
    from { width: 100%; }
    to { width: 0%; }
  }
`;
document.head.appendChild(style);
