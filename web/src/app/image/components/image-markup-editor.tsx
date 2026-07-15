"use client";

import {
  ArrowUpRight,
  Check,
  Eraser,
  Paintbrush,
  PenLine,
  Redo2,
  RotateCcw,
  Undo2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState, type PointerEvent as ReactPointerEvent } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import type {
  ImageMarkupAction,
  ImageMarkupPoint,
  ImageMarkupTool,
  StoredReferenceImage,
} from "@/store/image-conversations";

const MAX_EDITOR_EDGE = 2048;
const MASK_COLOR = "rgba(16, 185, 129, 1)";
const ANNOTATION_COLOR = "rgba(249, 82, 36, 1)";

type CanvasSize = {
  width: number;
  height: number;
};

type ImageMarkupEditorProps = {
  open: boolean;
  image: StoredReferenceImage | null;
  onOpenChange: (open: boolean) => void;
  onSave: (patch: Pick<StoredReferenceImage, "maskDataUrl" | "annotationDataUrl" | "markupActions">) => void;
};

type ImageMarkupEditorSessionProps = Omit<ImageMarkupEditorProps, "open" | "image"> & {
  image: StoredReferenceImage;
};

function createLayerCanvas(size: CanvasSize) {
  const canvas = document.createElement("canvas");
  canvas.width = size.width;
  canvas.height = size.height;
  return canvas;
}

function actionLineWidth(action: ImageMarkupAction, size: CanvasSize) {
  return Math.max(2, action.width * Math.min(size.width, size.height));
}

function drawStroke(
  context: CanvasRenderingContext2D,
  action: ImageMarkupAction,
  size: CanvasSize,
) {
  const points = action.points;
  if (points.length === 0) return;
  const lineWidth = actionLineWidth(action, size);
  context.lineWidth = lineWidth;
  context.lineCap = "round";
  context.lineJoin = "round";
  if (points.length === 1) {
    context.beginPath();
    context.arc(points[0].x * size.width, points[0].y * size.height, lineWidth / 2, 0, Math.PI * 2);
    context.fill();
    return;
  }
  context.beginPath();
  context.moveTo(points[0].x * size.width, points[0].y * size.height);
  for (const point of points.slice(1)) {
    context.lineTo(point.x * size.width, point.y * size.height);
  }
  context.stroke();
}

function drawArrow(
  context: CanvasRenderingContext2D,
  action: ImageMarkupAction,
  size: CanvasSize,
) {
  const start = action.points[0];
  const end = action.points.at(-1);
  if (!start || !end) return;
  const startX = start.x * size.width;
  const startY = start.y * size.height;
  const endX = end.x * size.width;
  const endY = end.y * size.height;
  const angle = Math.atan2(endY - startY, endX - startX);
  const lineWidth = actionLineWidth(action, size);
  const headLength = Math.max(lineWidth * 4.2, Math.min(size.width, size.height) * 0.025);

  context.lineWidth = lineWidth;
  context.lineCap = "round";
  context.lineJoin = "round";
  context.beginPath();
  context.moveTo(startX, startY);
  context.lineTo(endX, endY);
  context.stroke();
  context.beginPath();
  context.moveTo(endX, endY);
  context.lineTo(endX - headLength * Math.cos(angle - Math.PI / 6), endY - headLength * Math.sin(angle - Math.PI / 6));
  context.moveTo(endX, endY);
  context.lineTo(endX - headLength * Math.cos(angle + Math.PI / 6), endY - headLength * Math.sin(angle + Math.PI / 6));
  context.stroke();
}

function renderActions(
  size: CanvasSize,
  actions: ImageMarkupAction[],
) {
  const mask = createLayerCanvas(size);
  const annotation = createLayerCanvas(size);
  const maskContext = mask.getContext("2d");
  const annotationContext = annotation.getContext("2d");
  if (!maskContext || !annotationContext) {
    return { mask, annotation };
  }

  for (const action of actions) {
    if (action.tool === "mask") {
      maskContext.save();
      maskContext.globalCompositeOperation = "source-over";
      maskContext.strokeStyle = MASK_COLOR;
      maskContext.fillStyle = MASK_COLOR;
      drawStroke(maskContext, action, size);
      maskContext.restore();
      continue;
    }
    if (action.tool === "pen" || action.tool === "arrow") {
      annotationContext.save();
      annotationContext.globalCompositeOperation = "source-over";
      annotationContext.strokeStyle = ANNOTATION_COLOR;
      annotationContext.fillStyle = ANNOTATION_COLOR;
      if (action.tool === "arrow") {
        drawArrow(annotationContext, action, size);
      } else {
        drawStroke(annotationContext, action, size);
      }
      annotationContext.restore();
      continue;
    }
    if (action.tool === "eraser") {
      for (const context of [maskContext, annotationContext]) {
        context.save();
        context.globalCompositeOperation = "destination-out";
        context.strokeStyle = "rgba(0, 0, 0, 1)";
        context.fillStyle = "rgba(0, 0, 0, 1)";
        drawStroke(context, action, size);
        context.restore();
      }
    }
  }

  return { mask, annotation };
}

function layerHasPixels(canvas: HTMLCanvasElement) {
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return false;
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
  for (let index = 3; index < pixels.length; index += 4) {
    if (pixels[index] > 0) return true;
  }
  return false;
}

function normalizedPoint(event: ReactPointerEvent<HTMLCanvasElement>): ImageMarkupPoint {
  const bounds = event.currentTarget.getBoundingClientRect();
  return {
    x: Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width)),
    y: Math.min(1, Math.max(0, (event.clientY - bounds.top) / bounds.height)),
  };
}

function createAction(tool: ImageMarkupTool, width: number, point: ImageMarkupPoint): ImageMarkupAction {
  return {
    id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
    tool,
    width: Math.min(0.2, Math.max(0.002, width / 1000)),
    points: tool === "arrow" ? [point, point] : [point],
  };
}

const tools: Array<{ value: ImageMarkupTool; label: string; icon: typeof Paintbrush }> = [
  { value: "mask", label: "蒙版", icon: Paintbrush },
  { value: "pen", label: "钢笔", icon: PenLine },
  { value: "arrow", label: "箭头", icon: ArrowUpRight },
  { value: "eraser", label: "橡皮擦", icon: Eraser },
];

function cloneMarkupActions(image: StoredReferenceImage) {
  return image.markupActions?.map((action) => ({
    ...action,
    points: action.points.map((point) => ({ ...point })),
  })) || [];
}

function ImageMarkupEditorSession({ image, onOpenChange, onSave }: ImageMarkupEditorSessionProps) {
  const [sourceImage, setSourceImage] = useState<HTMLImageElement | null>(null);
  const [canvasSize, setCanvasSize] = useState<CanvasSize>({ width: 1, height: 1 });
  const [actions, setActions] = useState<ImageMarkupAction[]>(() => cloneMarkupActions(image));
  const [cursor, setCursor] = useState(() => image.markupActions?.length || 0);
  const [draftAction, setDraftAction] = useState<ImageMarkupAction | null>(null);
  const [tool, setTool] = useState<ImageMarkupTool>("mask");
  const [brushSize, setBrushSize] = useState(36);
  const [loadError, setLoadError] = useState("");

  const visibleActions = useMemo(
    () => [...actions.slice(0, cursor), ...(draftAction ? [draftAction] : [])],
    [actions, cursor, draftAction],
  );

  useEffect(() => {
    let active = true;
    const nextImage = new Image();
    nextImage.onload = () => {
      if (!active) return;
      const scale = Math.min(1, MAX_EDITOR_EDGE / Math.max(nextImage.naturalWidth, nextImage.naturalHeight));
      setCanvasSize({
        width: Math.max(1, Math.round(nextImage.naturalWidth * scale)),
        height: Math.max(1, Math.round(nextImage.naturalHeight * scale)),
      });
      setSourceImage(nextImage);
    };
    nextImage.onerror = () => {
      if (active) setLoadError("图片读取失败，请重新上传后再编辑");
    };
    nextImage.src = image.dataUrl;
    return () => {
      active = false;
    };
  }, [image.dataUrl]);

  const drawEditor = useCallback((canvas: HTMLCanvasElement | null) => {
    if (!canvas || !sourceImage) return;
    canvas.width = canvasSize.width;
    canvas.height = canvasSize.height;
    const context = canvas.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, canvasSize.width, canvasSize.height);
    context.drawImage(sourceImage, 0, 0, canvasSize.width, canvasSize.height);
    const layers = renderActions(canvasSize, visibleActions);
    context.save();
    context.globalAlpha = 0.42;
    context.drawImage(layers.mask, 0, 0);
    context.restore();
    context.drawImage(layers.annotation, 0, 0);
  }, [canvasSize, sourceImage, visibleActions]);

  const undo = useCallback(() => {
    setDraftAction(null);
    setCursor((current) => Math.max(0, current - 1));
  }, []);

  const redo = useCallback(() => {
    setDraftAction(null);
    setCursor((current) => Math.min(actions.length, current + 1));
  }, [actions.length]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      if (event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) redo();
        else undo();
      } else if (event.key.toLowerCase() === "y") {
        event.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [redo, undo]);

  const handlePointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!sourceImage) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    setDraftAction(createAction(tool, brushSize, normalizedPoint(event)));
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!draftAction || !event.currentTarget.hasPointerCapture(event.pointerId)) return;
    event.preventDefault();
    const point = normalizedPoint(event);
    setDraftAction((current) => {
      if (!current) return null;
      if (current.tool === "arrow") {
        return { ...current, points: [current.points[0], point] };
      }
      const previous = current.points.at(-1);
      if (previous && Math.hypot(point.x - previous.x, point.y - previous.y) < 0.0015) {
        return current;
      }
      return { ...current, points: [...current.points, point] };
    });
  };

  const finishPointer = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!draftAction) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    const nextActions = [...actions.slice(0, cursor), draftAction];
    setActions(nextActions);
    setCursor(nextActions.length);
    setDraftAction(null);
  };

  const save = () => {
    if (!sourceImage) return;
    const committedActions = actions.slice(0, cursor);
    const layers = renderActions(canvasSize, committedActions);
    const hasMask = layerHasPixels(layers.mask);
    const hasAnnotation = layerHasPixels(layers.annotation);

    let maskDataUrl: string | undefined;
    if (hasMask) {
      const mask = createLayerCanvas(canvasSize);
      const context = mask.getContext("2d");
      if (context) {
        context.fillStyle = "rgba(255, 255, 255, 1)";
        context.fillRect(0, 0, canvasSize.width, canvasSize.height);
        context.globalCompositeOperation = "destination-out";
        context.drawImage(layers.mask, 0, 0);
        maskDataUrl = mask.toDataURL("image/png");
      }
    }

    let annotationDataUrl: string | undefined;
    if (hasAnnotation) {
      const guide = createLayerCanvas(canvasSize);
      const context = guide.getContext("2d");
      if (context) {
        context.drawImage(sourceImage, 0, 0, canvasSize.width, canvasSize.height);
        context.drawImage(layers.annotation, 0, 0);
        annotationDataUrl = guide.toDataURL("image/png");
      }
    }

    onSave({
      maskDataUrl,
      annotationDataUrl,
      markupActions: (hasMask || hasAnnotation) && committedActions.length ? committedActions : undefined,
    });
    onOpenChange(false);
  };

  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogContent
        showCloseButton={false}
        className="grid h-[min(95dvh,980px)] w-[min(97vw,1380px)] max-w-none grid-rows-[auto_auto_minmax(0,1fr)_auto] gap-0 overflow-hidden rounded-[24px] border-stone-700 bg-stone-950 p-0 text-white shadow-[0_40px_140px_-32px_rgba(0,0,0,0.85)] sm:rounded-[32px]"
      >
        <DialogHeader className="flex-row items-center justify-between border-b border-white/10 px-4 py-3 sm:px-6 sm:py-4">
          <div className="min-w-0">
            <DialogTitle className="truncate text-base font-semibold text-white sm:text-lg">局部编辑 · {image?.name || "参考图"}</DialogTitle>
            <DialogDescription className="mt-1 text-xs text-stone-400">
              绿色区域会被修改；橙红色钢笔和箭头只用于告诉模型位置。
            </DialogDescription>
          </div>
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="inline-flex size-9 shrink-0 items-center justify-center rounded-full text-stone-400 transition hover:bg-white/10 hover:text-white"
            aria-label="关闭图片编辑器"
          >
            <X className="size-5" />
          </button>
        </DialogHeader>

        <div className="hide-scrollbar flex items-center gap-2 overflow-x-auto border-b border-white/10 px-3 py-2 sm:px-6 sm:py-3">
          <div className="flex shrink-0 items-center gap-1 rounded-xl bg-white/5 p-1">
            {tools.map((item) => {
              const Icon = item.icon;
              return (
                <button
                  key={item.value}
                  type="button"
                  onClick={() => setTool(item.value)}
                  className={cn(
                    "inline-flex h-9 items-center gap-1.5 rounded-lg px-2.5 text-xs font-medium transition sm:px-3",
                    tool === item.value ? "bg-white text-stone-950" : "text-stone-300 hover:bg-white/10 hover:text-white",
                  )}
                  aria-pressed={tool === item.value}
                >
                  <Icon className="size-4" />
                  {item.label}
                </button>
              );
            })}
          </div>
          <label className="flex min-w-44 shrink-0 items-center gap-2 rounded-xl bg-white/5 px-3 py-2 text-xs text-stone-300">
            <span>粗细</span>
            <input
              type="range"
              min="8"
              max="120"
              step="2"
              value={brushSize}
              onChange={(event) => setBrushSize(Number(event.target.value))}
              className="w-24 accent-emerald-400"
            />
            <span className="w-7 text-right tabular-nums">{brushSize}</span>
          </label>
          <div className="ml-auto flex shrink-0 items-center gap-1">
            <button type="button" onClick={undo} disabled={cursor === 0} className="inline-flex size-9 items-center justify-center rounded-xl text-stone-300 transition hover:bg-white/10 disabled:opacity-30" aria-label="撤回" title="撤回 Ctrl/⌘+Z">
              <Undo2 className="size-4" />
            </button>
            <button type="button" onClick={redo} disabled={cursor >= actions.length} className="inline-flex size-9 items-center justify-center rounded-xl text-stone-300 transition hover:bg-white/10 disabled:opacity-30" aria-label="反撤回" title="反撤回 Ctrl/⌘+Shift+Z">
              <Redo2 className="size-4" />
            </button>
            <button
              type="button"
              onClick={() => {
                setActions([]);
                setCursor(0);
                setDraftAction(null);
              }}
              disabled={cursor === 0}
              className="inline-flex h-9 items-center gap-1.5 rounded-xl px-2.5 text-xs text-stone-300 transition hover:bg-white/10 disabled:opacity-30"
            >
              <RotateCcw className="size-4" />
              清除
            </button>
          </div>
        </div>

        <div className="flex min-h-0 items-center justify-center overflow-auto bg-[radial-gradient(circle_at_center,rgba(63,63,70,0.7),rgba(9,9,11,1)_68%)] p-3 sm:p-6">
          {loadError ? (
            <div className="rounded-2xl border border-rose-400/30 bg-rose-500/10 px-5 py-4 text-sm text-rose-200">{loadError}</div>
          ) : sourceImage ? (
            <canvas
              ref={drawEditor}
              onPointerDown={handlePointerDown}
              onPointerMove={handlePointerMove}
              onPointerUp={finishPointer}
              onPointerCancel={finishPointer}
              onContextMenu={(event) => event.preventDefault()}
              className={cn(
                "block max-h-full max-w-full touch-none rounded-lg bg-white shadow-2xl shadow-black/60",
                tool === "eraser" ? "cursor-cell" : "cursor-crosshair",
              )}
              aria-label="图片蒙版和标注画布"
            />
          ) : (
            <div className="text-sm text-stone-400">正在加载图片…</div>
          )}
        </div>

        <div className="flex flex-col gap-3 border-t border-white/10 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-6 sm:py-4">
          <div className="flex flex-wrap items-center gap-3 text-xs text-stone-400">
            <span className="inline-flex items-center gap-1.5"><span className="size-2.5 rounded-full bg-emerald-500" />蒙版：透明后发送，限定修改区域</span>
            <span className="inline-flex items-center gap-1.5"><span className="size-2.5 rounded-full bg-[#f95224]" />标注：作为额外定位参考，不写入原图</span>
          </div>
          <div className="flex items-center justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} className="h-10 rounded-xl border-white/15 bg-transparent text-stone-200 hover:bg-white/10 hover:text-white">
              取消
            </Button>
            <Button type="button" onClick={save} disabled={!sourceImage || Boolean(loadError)} className="h-10 rounded-xl bg-emerald-400 px-5 font-semibold text-stone-950 hover:bg-emerald-300">
              <Check className="size-4" />
              应用蒙版与标注
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function ImageMarkupEditor({ open, image, onOpenChange, onSave }: ImageMarkupEditorProps) {
  if (!open || !image) return null;
  return (
    <ImageMarkupEditorSession
      key={`${image.name}:${image.dataUrl.length}:${image.dataUrl.slice(-24)}`}
      image={image}
      onOpenChange={onOpenChange}
      onSave={onSave}
    />
  );
}
