import { useEffect, useRef, useState } from 'react';
import Icon from './Icon';

/**
 * Fullscreen image viewer for visual evidence (UI spec §66): zoom,
 * next/previous, close, and the image's source/context.
 */
export default function Lightbox({ images, index, onIndex, onClose }) {
  // Zoom resets when the image changes: it is stored together with the index it belongs to.
  const [zoomState, setZoomState] = useState({ index, zoom: 1 });
  const zoom = zoomState.index === index ? zoomState.zoom : 1;
  const setZoom = (update) => setZoomState((current) => {
    const base = current.index === index ? current.zoom : 1;
    return { index, zoom: typeof update === 'function' ? update(base) : update };
  });
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const image = images[index];

  useEffect(() => {
    const previouslyFocused = document.activeElement;
    closeRef.current?.focus();
    return () => previouslyFocused?.focus?.();
  }, []);

  useEffect(() => {
    function onKey(event) {
      if (event.key === 'Escape') onClose();
      else if (event.key === 'ArrowRight') onIndex((index + 1) % images.length);
      else if (event.key === 'ArrowLeft') onIndex((index - 1 + images.length) % images.length);
      else if (event.key === '+' || event.key === '=' || event.key === '-') {
        const delta = event.key === '-' ? -0.5 : 0.5;
        setZoomState((current) => {
          const base = current.index === index ? current.zoom : 1;
          return { index, zoom: Math.min(4, Math.max(1, base + delta)) };
        });
      }
      else if (event.key === 'Tab') {
        const focusable = dialogRef.current?.querySelectorAll('button, a[href]');
        if (!focusable?.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [index, images.length, onClose, onIndex]);

  if (!image) return null;
  return (
    <div className="lightbox" role="dialog" aria-modal="true" aria-label={`${image.label} of ${images.length}`} ref={dialogRef}>
      <div className="lightbox-toolbar">
        <span className="lightbox-caption">{image.label} of {images.length}{image.context ? ` · ${image.context}` : ''}</span>
        <button type="button" onClick={() => setZoom((value) => Math.max(1, value - 0.5))} disabled={zoom <= 1} aria-label="Zoom out">−</button>
        <span aria-live="polite" className="lightbox-zoom">{Math.round(zoom * 100)}%</span>
        <button type="button" onClick={() => setZoom((value) => Math.min(4, value + 0.5))} disabled={zoom >= 4} aria-label="Zoom in">+</button>
        {image.href && <a href={image.href} target="_blank" rel="noreferrer noopener">Open original</a>}
        <button type="button" ref={closeRef} onClick={onClose} aria-label="Close viewer"><Icon name="close" size={18} /></button>
      </div>
      <div className="lightbox-stage">
        <img src={image.src} alt={image.alt} style={{ transform: `scale(${zoom})` }} />
      </div>
      {images.length > 1 && (
        <>
          <button type="button" className="lightbox-nav prev" onClick={() => onIndex((index - 1 + images.length) % images.length)} aria-label="Previous image">‹</button>
          <button type="button" className="lightbox-nav next" onClick={() => onIndex((index + 1) % images.length)} aria-label="Next image">›</button>
        </>
      )}
    </div>
  );
}
