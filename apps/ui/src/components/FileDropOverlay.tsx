import { useEffect, useState } from "react";
import { Ban } from "lucide-react";

/**
 * Global drag-and-drop guard.
 *
 * Whenever the user drags a file over the window, this shows a full-screen
 * overlay. Over a registered drop zone (any element marked with
 * `[data-drop-zone]`) the overlay stays out of the way so the zone's own
 * affordance shows; everywhere else it displays a "no drop" (circle-with-slash)
 * symbol and prevents the browser from navigating to/open the dragged file.
 *
 * Drop zones opt in by adding the `data-drop-zone` attribute and handling their
 * own `onDrop`; this component only owns the negative-space visual and the
 * window-level `preventDefault`.
 */
export default function FileDropOverlay() {
  // A file is currently being dragged somewhere over the window.
  const [dragging, setDragging] = useState(false);
  // The pointer is over a registered drop zone, so dropping is allowed.
  const [overZone, setOverZone] = useState(false);

  useEffect(() => {
    // dragenter/dragleave fire for every nested element, so a counter is the
    // reliable way to know when the drag truly entered/left the window.
    let depth = 0;

    const hasFiles = (event: DragEvent) =>
      Boolean(event.dataTransfer?.types?.includes("Files"));

    const isOverZone = (event: DragEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return false;
      return Boolean(target.closest("[data-drop-zone]"));
    };

    const onDragEnter = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      depth += 1;
      setDragging(true);
      setOverZone(isOverZone(event));
    };

    const onDragOver = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      // preventDefault is required both to enable `drop` on zones and to stop
      // the browser from opening/navigating to the file when dropped anywhere.
      event.preventDefault();
      setOverZone(isOverZone(event));
    };

    const onDragLeave = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      depth -= 1;
      if (depth <= 0) {
        depth = 0;
        setDragging(false);
        setOverZone(false);
      }
    };

    const finish = (event: DragEvent) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      depth = 0;
      setDragging(false);
      setOverZone(false);
    };

    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", finish);
    window.addEventListener("dragend", finish);
    return () => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", finish);
      window.removeEventListener("dragend", finish);
    };
  }, []);

  if (!dragging || overZone) return null;

  return (
    <div
      aria-hidden
      className="pointer-events-none fixed inset-0 z-[100] flex items-center justify-center bg-danger-subtle/60 backdrop-blur-[1px]"
      style={{ cursor: "no-drop" }}
    >
      <div className="flex flex-col items-center gap-3 rounded-3xl border-2 border-dashed border-danger bg-surface-1 px-10 py-8 shadow-lg">
        <Ban className="size-12 text-danger" aria-hidden />
        <p className="text-sm font-medium text-danger">Drop onto a file zone to upload</p>
      </div>
    </div>
  );
}
