"use client";
import { ArrowUp, BookOpenText, ChevronDown, ImagePlus, Images, Info, LoaderCircle, RectangleHorizontal, RectangleVertical, SlidersHorizontal, Sparkles, Square, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ClipboardEvent, type DragEvent, type RefObject } from "react";
import { toast } from "sonner";

import { ImageMarkupEditor } from "@/app/image/components/image-markup-editor";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  getEffectiveChatModel,
  streamChatCompletion,
  type ImageChatConfig,
} from "@/app/image/lib/chat-completions";
import type { ImageModel } from "@/lib/api";
import type { MaterialLibraryItem, PromptLibraryItem } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { StoredReferenceImage } from "@/store/image-conversations";

type ImageComposerProps = {
  prompt: string;
  imageCount: string;
  imageRatio: string;
  imageTier: string;
  imageWidth: string;
  imageHeight: string;
  imageQuality: string;
  imageModel: ImageModel;
  imageModels: ImageModel[];
  accountPool: "default" | "gptfree";
  chatConfig: ImageChatConfig;
  promptLibraryItems: PromptLibraryItem[];
  materialLibraryItems: MaterialLibraryItem[];
  selectedPromptIds: string[];
  selectedMaterialIds: string[];
  availableQuota: string;
  activeTaskCount: number;
  referenceImages: StoredReferenceImage[];
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  fileInputRef: RefObject<HTMLInputElement | null>;
  onPromptChange: (value: string) => void;
  onImageCountChange: (value: string) => void;
  onImageRatioChange: (value: string) => void;
  onImageTierChange: (value: string) => void;
  onImageWidthChange: (value: string) => void;
  onImageHeightChange: (value: string) => void;
  onImageQualityChange: (value: string) => void;
  onImageModelChange: (value: ImageModel) => void;
  onAccountPoolChange: (value: "default" | "gptfree") => void;
  onTogglePromptSelection: (id: string) => void;
  onToggleMaterialSelection: (id: string) => void;
  onSubmit: () => void | Promise<void>;
  onPickReferenceImage: () => void;
  onReferenceImageChange: (files: File[]) => void | Promise<void>;
  onRemoveReferenceImage: (index: number) => void;
  onUpdateReferenceImage: (index: number, patch: Partial<StoredReferenceImage>) => void;
};

const imageFileNamePattern = /\.(avif|bmp|gif|heic|heif|ico|jpe?g|png|svg|tiff?|webp)$/i;
const OPTIMIZE_PROMPT_STORAGE_KEY = "chatgpt2api:image_optimize_instruction";
const DEFAULT_OPTIMIZE_INSTRUCTION = "请帮我优化这个提示词以达到更专业更优秀的效果，如果有短板帮我优化。";

function isImageFile(file: File) {
  return file.type.startsWith("image/") || (!file.type && imageFileNamePattern.test(file.name));
}

function hasDraggedImages(dataTransfer: DataTransfer) {
  const items = Array.from(dataTransfer.items || []);
  if (items.length > 0) {
    return items.some((item) => item.kind === "file" && (item.type.startsWith("image/") || !item.type));
  }
  return Array.from(dataTransfer.files || []).some(isImageFile);
}

function getDraggedImageFiles(dataTransfer: DataTransfer) {
  return Array.from(dataTransfer.files || []).filter(isImageFile);
}

const qualityOptions = [
  { value: "auto", label: "自动" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];
const aspectOptions = [
  { ratio: "1:1", tier: "1k", width: "1024", height: "1024", label: "1:1", icon: Square },
  { ratio: "2:3", tier: "1k", width: "1024", height: "1536", label: "2:3", icon: RectangleVertical },
  { ratio: "3:2", tier: "1k", width: "1536", height: "1024", label: "3:2", icon: RectangleHorizontal },
  { ratio: "3:4", tier: "1k", width: "1024", height: "1365", label: "3:4", icon: RectangleVertical },
  { ratio: "4:3", tier: "1k", width: "1365", height: "1024", label: "4:3", icon: RectangleHorizontal },
  { ratio: "9:16", tier: "1k", width: "1088", height: "1920", label: "9:16", icon: RectangleVertical },
  { ratio: "16:9", tier: "1k", width: "1920", height: "1088", label: "16:9", icon: RectangleHorizontal },
  { ratio: "1:1", tier: "2k", width: "2048", height: "2048", label: "1:1(2k)", icon: Square },
  { ratio: "16:9", tier: "2k", width: "2560", height: "1440", label: "16:9(2k)", icon: RectangleHorizontal },
  { ratio: "9:16", tier: "2k", width: "1440", height: "2560", label: "9:16(2k)", icon: RectangleVertical },
  { ratio: "16:9", tier: "4k", width: "3840", height: "2160", label: "16:9(4k)", icon: RectangleHorizontal },
  { ratio: "9:16", tier: "4k", width: "2160", height: "3840", label: "9:16(4k)", icon: RectangleVertical },
  { ratio: "auto", tier: "auto", width: "1024", height: "1024", label: "auto", icon: null },
];
const countOptions = Array.from({ length: 10 }, (_, index) => String(index + 1));

export function ImageComposer({
  prompt,
  imageCount,
  imageRatio,
  imageTier,
  imageWidth,
  imageHeight,
  imageQuality,
  imageModel,
  imageModels,
  accountPool,
  chatConfig,
  promptLibraryItems,
  materialLibraryItems,
  selectedPromptIds,
  selectedMaterialIds,
  availableQuota,
  activeTaskCount,
  referenceImages,
  textareaRef,
  fileInputRef,
  onPromptChange,
  onImageCountChange,
  onImageRatioChange,
  onImageTierChange,
  onImageWidthChange,
  onImageHeightChange,
  onImageQualityChange,
  onImageModelChange,
  onAccountPoolChange,
  onTogglePromptSelection,
  onToggleMaterialSelection,
  onSubmit,
  onPickReferenceImage,
  onReferenceImageChange,
  onRemoveReferenceImage,
  onUpdateReferenceImage,
}: ImageComposerProps) {
  const [editingReferenceIndex, setEditingReferenceIndex] = useState<number | null>(null);
  const [isSizeMenuOpen, setIsSizeMenuOpen] = useState(false);
  const [isDraggingImage, setIsDraggingImage] = useState(false);
  const [isOptimizing, setIsOptimizing] = useState(false);
  const [isOptimizeConfigOpen, setIsOptimizeConfigOpen] = useState(false);
  const [optimizeInstruction, setOptimizeInstruction] = useState(DEFAULT_OPTIMIZE_INSTRUCTION);
  const [sizeMenuPos, setSizeMenuPos] = useState<{ top: number; left: number }>({ top: 0, left: 0 });
  const sizeMenuRef = useRef<HTMLDivElement>(null);
  const sizeMenuBtnRef = useRef<HTMLButtonElement>(null);
  const optimizeAbortRef = useRef<AbortController | null>(null);
  const editingReferenceImage = editingReferenceIndex === null ? null : referenceImages[editingReferenceIndex] || null;
  const modelOptions = useMemo(
    () => imageModels.map((model) => ({ value: model, label: model })),
    [imageModels],
  );
  const qualityLabel = qualityOptions.find((option) => option.value === imageQuality)?.label || "自动";
  const ratioLabel = imageRatio === "auto" ? "auto" : `${imageRatio}(${imageTier})`;
  const poolLabel = accountPool === "gptfree" ? "gptFree号池" : "默认号池";
  const imageSizeLabel = `${poolLabel} · ${qualityLabel} · ${ratioLabel} · ${imageCount || 1} 张`;
  const selectedModelLabel = modelOptions.find((option) => option.value === imageModel)?.label || imageModel;
  const isCodexModel = imageModel.toLowerCase().includes("codex");
  const selectedPromptCount = selectedPromptIds.length;
  const selectedMaterialCount = selectedMaterialIds.length;
  const promptLibraryPicker = (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          className={cn(
            "h-8 shrink-0 rounded-xl border-stone-200 bg-white/95 px-2.5 text-xs font-medium text-stone-700 shadow-sm backdrop-blur sm:h-9 sm:px-3.5",
            selectedPromptCount > 0 && "border-stone-950 bg-stone-950 text-white hover:bg-stone-800",
          )}
        >
          <BookOpenText className="size-3.5" />
          <span className="hidden sm:inline">提示词</span>
          {selectedPromptCount > 0 ? <span>{selectedPromptCount}</span> : null}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[min(420px,calc(100vw-2rem))] p-3">
        <div className="mb-2 text-sm font-semibold text-stone-900">选择默认提示词</div>
        <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
          {promptLibraryItems.length === 0 ? (
            <div className="rounded-xl border border-dashed border-stone-200 bg-stone-50 px-3 py-6 text-center text-sm text-stone-500">暂无提示词</div>
          ) : (
            promptLibraryItems.map((item) => {
              const active = selectedPromptIds.includes(item.id);
              return (
                <button
                  key={item.id}
                  type="button"
                  className={cn(
                    "w-full rounded-xl border px-3 py-2 text-left transition",
                    active ? "border-stone-950 bg-stone-950 text-white" : "border-stone-200 bg-white text-stone-800 hover:bg-stone-50",
                  )}
                  onClick={() => onTogglePromptSelection(item.id)}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium">{item.name}</span>
                    <span className={cn("shrink-0 rounded-lg px-2 py-0.5 text-xs", active ? "bg-white/15 text-white" : "bg-stone-100 text-stone-500")}>{item.type}</span>
                  </div>
                  <div className={cn("mt-1 line-clamp-2 text-xs leading-5", active ? "text-white/75" : "text-stone-500")}>{item.content}</div>
                </button>
              );
            })
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
  const materialLibraryPicker = (
    <Popover>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          className={cn(
            "h-8 shrink-0 rounded-xl border-stone-200 bg-white/95 px-2.5 text-xs font-medium text-stone-700 shadow-sm backdrop-blur sm:h-9 sm:px-3.5",
            selectedMaterialCount > 0 && "border-stone-950 bg-stone-950 text-white hover:bg-stone-800",
          )}
        >
          <Images className="size-3.5" />
          <span className="hidden sm:inline">素材</span>
          {selectedMaterialCount > 0 ? <span>{selectedMaterialCount}</span> : null}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[min(460px,calc(100vw-2rem))] p-3">
        <div className="mb-2 text-sm font-semibold text-stone-900">选择默认素材</div>
        <div className="grid max-h-80 grid-cols-2 gap-2 overflow-y-auto pr-1 sm:grid-cols-3">
          {materialLibraryItems.length === 0 ? (
            <div className="col-span-full rounded-xl border border-dashed border-stone-200 bg-stone-50 px-3 py-6 text-center text-sm text-stone-500">暂无素材</div>
          ) : (
            materialLibraryItems.map((item) => {
              const active = selectedMaterialIds.includes(item.id);
              return (
                <button
                  key={item.id}
                  type="button"
                  className={cn(
                    "overflow-hidden rounded-xl border bg-white text-left transition",
                    active ? "border-stone-950 ring-2 ring-stone-950/15" : "border-stone-200 hover:border-stone-300",
                  )}
                  onClick={() => onToggleMaterialSelection(item.id)}
                >
                  <div className="aspect-square bg-stone-100">
                    <img src={item.thumbnail_url || item.url} alt={item.name} className="h-full w-full object-cover" />
                  </div>
                  <div className="p-2">
                    <div className="truncate text-xs font-medium text-stone-800">{item.name}</div>
                    <div className="mt-0.5 truncate text-[11px] text-stone-500">{item.type}</div>
                  </div>
                </button>
              );
            })
          )}
        </div>
      </PopoverContent>
    </Popover>
  );

  useEffect(() => {
    try {
      const stored = window.localStorage.getItem(OPTIMIZE_PROMPT_STORAGE_KEY);
      if (stored?.trim()) {
        setOptimizeInstruction(stored);
      }
    } catch {
      // localStorage may be unavailable in privacy contexts.
    }
  }, []);

  useEffect(() => {
    return () => {
      optimizeAbortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!isSizeMenuOpen) {
      return;
    }
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target;
      if (
        target instanceof Element &&
        target.closest('[data-slot="select-content"], [data-slot="select-trigger"]')
      ) {
        return;
      }
      if (!sizeMenuRef.current?.contains(target as Node)) {
        setIsSizeMenuOpen(false);
      }
    };
    window.addEventListener("mousedown", handlePointerDown);
    return () => {
      window.removeEventListener("mousedown", handlePointerDown);
    };
  }, [isSizeMenuOpen]);

  const handleTextareaPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const imageFiles = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith("image/"));
    if (imageFiles.length === 0) {
      return;
    }

    event.preventDefault();
    void onReferenceImageChange(imageFiles);
  };

  const handleComposerDragEnter = (event: DragEvent<HTMLDivElement>) => {
    if (!hasDraggedImages(event.dataTransfer)) {
      return;
    }

    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setIsSizeMenuOpen(false);
    setIsDraggingImage(true);
  };

  const handleComposerDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (!hasDraggedImages(event.dataTransfer)) {
      return;
    }

    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setIsDraggingImage(true);
  };

  const handleComposerDragLeave = (event: DragEvent<HTMLDivElement>) => {
    const nextTarget = event.relatedTarget;
    if (nextTarget instanceof Node && event.currentTarget.contains(nextTarget)) {
      return;
    }
    setIsDraggingImage(false);
  };

  const handleComposerDrop = (event: DragEvent<HTMLDivElement>) => {
    const imageFiles = getDraggedImageFiles(event.dataTransfer);
    if (event.dataTransfer.files.length > 0 || imageFiles.length > 0) {
      event.preventDefault();
      event.stopPropagation();
    }

    setIsDraggingImage(false);
    if (imageFiles.length === 0) {
      return;
    }

    void onReferenceImageChange(imageFiles);
  };

  const handleOptimizeInstructionChange = (value: string) => {
    setOptimizeInstruction(value);
    try {
      window.localStorage.setItem(OPTIMIZE_PROMPT_STORAGE_KEY, value);
    } catch {
      // localStorage may be full or unavailable.
    }
  };

  const resetOptimizeInstruction = () => {
    handleOptimizeInstructionChange(DEFAULT_OPTIMIZE_INSTRUCTION);
  };

  const handleOptimizePrompt = async () => {
    if (isOptimizing) {
      optimizeAbortRef.current?.abort();
      optimizeAbortRef.current = null;
      setIsOptimizing(false);
      toast.info("已停止优化");
      return;
    }

    const sourcePrompt = prompt.trim();
    if (!sourcePrompt) {
      textareaRef.current?.focus();
      toast.error("请先输入要优化的提示词");
      return;
    }

    const controller = new AbortController();
    optimizeAbortRef.current = controller;
    setIsOptimizing(true);

    let optimized = "";
    const instruction = optimizeInstruction.trim() || DEFAULT_OPTIMIZE_INSTRUCTION;
    try {
      await streamChatCompletion(
        {
          model: getEffectiveChatModel(chatConfig),
          messages: [
            {
              role: "system",
              content: `${instruction}\n\n请只输出优化后的生图提示词，不要解释，不要使用 Markdown，不要添加前后缀。`,
            },
            {
              role: "user",
              content: `原始生图提示词：\n${sourcePrompt}`,
            },
          ],
          stream: true,
          account_pool: chatConfig.accountPool,
          ...(chatConfig.reasoningEffort !== "default" ? { reasoning_effort: chatConfig.reasoningEffort } : {}),
        },
        controller.signal,
        (delta) => {
          optimized += delta;
        },
      );

      const nextPrompt = optimized.trim();
      if (!nextPrompt) {
        throw new Error("模型未返回优化结果");
      }
      onPromptChange(nextPrompt);
      requestAnimationFrame(() => textareaRef.current?.focus());
      toast.success("提示词已优化");
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        return;
      }
      const message = err instanceof Error ? err.message : String(err);
      toast.error(message || "优化失败");
    } finally {
      if (optimizeAbortRef.current === controller) {
        optimizeAbortRef.current = null;
      }
      setIsOptimizing(false);
    }
  };

  return (
    <div className="shrink-0 flex justify-center px-1 sm:px-0">
      <div style={{ width: "min(980px, 100%)" }}>
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          className="hidden"
          onChange={(event) => {
            void onReferenceImageChange(Array.from(event.target.files || []));
          }}
        />
        <ImageMarkupEditor
          open={editingReferenceIndex !== null}
          image={editingReferenceImage}
          onOpenChange={(open) => {
            if (!open) setEditingReferenceIndex(null);
          }}
          onSave={(patch) => {
            if (editingReferenceIndex === null) return;
            onUpdateReferenceImage(editingReferenceIndex, patch);
            toast.success("蒙版与标注已应用，发送时会随参考图一起上传");
          }}
        />

        {referenceImages.length > 0 ? (
          <div className="mb-2 flex gap-2 overflow-x-auto px-1 pb-1 sm:mb-3 sm:flex-wrap sm:overflow-visible sm:pb-0">
            {referenceImages.map((image, index) => (
              <div key={`${image.name}-${index}`} className="relative size-14 shrink-0 sm:size-16">
                <button
                  type="button"
                  onClick={() => setEditingReferenceIndex(index)}
                  className="group relative size-14 overflow-hidden rounded-2xl border border-stone-200 bg-stone-50 transition hover:border-emerald-400 hover:ring-2 hover:ring-emerald-100 sm:size-16"
                  aria-label={`编辑参考图蒙版和标注 ${image.name || index + 1}`}
                >
                  <img
                    src={image.markupPreviewDataUrl || image.annotationDataUrl || image.dataUrl}
                    alt={image.name || `参考图 ${index + 1}`}
                    className="h-full w-full object-cover"
                  />
                  <span className="absolute inset-x-1 bottom-1 translate-y-5 rounded-md bg-stone-950/80 py-0.5 text-[9px] font-medium text-white transition group-hover:translate-y-0">
                    涂抹编辑
                  </span>
                </button>
                {image.maskDataUrl || image.annotationDataUrl || image.markupPreviewDataUrl ? (
                  <div className="pointer-events-none absolute -bottom-1 left-1/2 flex -translate-x-1/2 gap-1">
                    {image.maskDataUrl ? <span className="rounded-full bg-emerald-500 px-1.5 py-0.5 text-[8px] font-semibold leading-none text-white shadow">蒙版</span> : null}
                    {image.annotationDataUrl ? <span className="rounded-full bg-orange-500 px-1.5 py-0.5 text-[8px] font-semibold leading-none text-white shadow">标注</span> : null}
                  </div>
                ) : null}
                <button
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation();
                    onRemoveReferenceImage(index);
                  }}
                  className="absolute -right-1 -top-1 inline-flex size-5 items-center justify-center rounded-full border border-stone-200 bg-white text-stone-500 transition hover:border-stone-300 hover:text-stone-800"
                  aria-label={`移除参考图 ${image.name || index + 1}`}
                >
                  <X className="size-3" />
                </button>
              </div>
            ))}
          </div>
        ) : null}

        <div
          className={cn(
            "overflow-hidden rounded-2xl border border-stone-200/90 bg-white shadow-[0_18px_70px_-48px_rgba(15,23,42,0.45)] transition dark:border-white/10 dark:bg-stone-950/80",
            isDraggingImage && "border-stone-900 bg-stone-50",
          )}
        >
          <div
            className="relative cursor-text"
            onDragEnter={handleComposerDragEnter}
            onDragOver={handleComposerDragOver}
            onDragLeave={handleComposerDragLeave}
            onDrop={handleComposerDrop}
            onClick={() => {
              textareaRef.current?.focus();
            }}
          >
            <div
              className="absolute right-3 top-3 z-10 flex max-w-[calc(100%-1.5rem)] items-center gap-1.5 sm:right-4 sm:top-4 sm:max-w-[calc(100%-2rem)]"
              onClick={(event) => event.stopPropagation()}
            >
              {promptLibraryPicker}
              {materialLibraryPicker}
              <button
                type="button"
                onClick={() => void handleOptimizePrompt()}
                disabled={!prompt.trim() && !isOptimizing}
                className="inline-flex h-8 items-center gap-1.5 rounded-xl border border-stone-200 bg-white/95 px-3 text-xs font-medium text-stone-700 shadow-sm backdrop-blur transition hover:border-stone-300 hover:bg-white disabled:cursor-not-allowed disabled:text-stone-300 sm:h-9 sm:px-3.5"
                title={isOptimizing ? "停止优化" : "优化提示词"}
              >
                {isOptimizing ? <LoaderCircle className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
                <span>{isOptimizing ? "优化中" : "优化"}</span>
              </button>
              <Popover open={isOptimizeConfigOpen} onOpenChange={setIsOptimizeConfigOpen}>
                <PopoverTrigger asChild>
                  <button
                    type="button"
                    className="inline-flex size-8 items-center justify-center rounded-xl border border-stone-200 bg-white/95 text-stone-500 shadow-sm backdrop-blur transition hover:border-stone-300 hover:bg-white hover:text-stone-800 sm:size-9"
                    title="优化配置"
                    aria-label="优化配置"
                  >
                    <SlidersHorizontal className="size-3.5" />
                  </button>
                </PopoverTrigger>
                <PopoverContent align="end" className="w-[min(360px,calc(100vw-2rem))] p-4">
                  <div className="mb-3 flex items-center justify-between gap-3">
                    <div>
                      <div className="text-sm font-semibold text-stone-900">优化提示词</div>
                      <div className="text-xs text-stone-500">使用右侧聊天的模型和思考配置</div>
                    </div>
                    <button
                      type="button"
                      className="shrink-0 rounded-full bg-stone-100 px-2.5 py-1 text-xs font-medium text-stone-600 transition hover:bg-stone-200"
                      onClick={resetOptimizeInstruction}
                    >
                      重置
                    </button>
                  </div>
                  <Textarea
                    value={optimizeInstruction}
                    onChange={(event) => handleOptimizeInstructionChange(event.target.value)}
                    className="min-h-28 resize-none rounded-xl border-stone-200 bg-white text-sm leading-6 shadow-none"
                  />
                  <div className="mt-2 text-xs leading-5 text-stone-500">
                    当前号池：{chatConfig.accountPool === "gptfree" ? "gptFree" : "默认"}，模型：{getEffectiveChatModel(chatConfig)}，思考：{chatConfig.reasoningEffort === "default" ? "默认" : chatConfig.reasoningEffort}
                  </div>
                </PopoverContent>
              </Popover>
            </div>
            <Textarea
              ref={textareaRef}
              value={prompt}
              onChange={(event) => onPromptChange(event.target.value)}
              onPaste={handleTextareaPaste}
              placeholder={
                referenceImages.length > 0
                  ? "描述你希望如何修改参考图"
                  : "输入你想要生成的画面，也可直接粘贴图片"
              }
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void onSubmit();
                }
              }}
              className="min-h-[82px] resize-none rounded-2xl border-0 bg-transparent px-4 pb-2 pr-28 pt-14 text-[15px] leading-6 text-stone-900 shadow-none placeholder:text-stone-400 focus-visible:ring-0 dark:text-stone-100 dark:placeholder:text-stone-500 sm:min-h-[148px] sm:px-6 sm:pb-20 sm:pr-36 sm:pt-16 sm:leading-7"
            />
            {isDraggingImage ? (
              <div className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center rounded-[24px] border-2 border-dashed border-stone-900 bg-white/85 text-sm font-medium text-stone-900 backdrop-blur-[1px] sm:rounded-[32px]">
                <div className="flex items-center gap-2 rounded-full bg-stone-950 px-4 py-2 text-white shadow-lg">
                  <ImagePlus className="size-4" />
                  <span>松开以上传参考图</span>
                </div>
              </div>
            ) : null}

            <div className="rounded-b-2xl border-t border-stone-100 bg-white px-3 pb-3 pt-2 dark:border-white/10 dark:bg-stone-950/95 sm:absolute sm:inset-x-0 sm:bottom-0 sm:border-t-0 sm:bg-gradient-to-t sm:from-white sm:via-white/95 sm:to-transparent sm:px-6 sm:pb-4 sm:pt-6 sm:dark:from-stone-950 sm:dark:via-stone-950/95 sm:dark:to-stone-950/0" onClick={(event) => event.stopPropagation()}>
              <div className="flex items-end justify-between gap-2 sm:gap-3">
                <div className="hide-scrollbar flex min-w-0 flex-1 flex-nowrap items-center gap-1.5 overflow-x-auto pb-0.5 sm:flex-wrap sm:gap-3 sm:overflow-visible sm:pb-0">
                  <Button
                    type="button"
                    variant="outline"
                    className="h-9 shrink-0 rounded-xl border-stone-200 bg-white px-3 text-xs font-medium text-stone-700 shadow-none sm:h-10 sm:px-4 sm:text-sm"
                    onClick={onPickReferenceImage}
                    aria-label={referenceImages.length > 0 ? "添加参考图" : "上传"}
                  >
                    <ImagePlus className="size-3.5 sm:size-4" />
                    <span className="hidden sm:inline">{referenceImages.length > 0 ? "添加参考图" : "上传"}</span>
                  </Button>
                  <div className="shrink-0 rounded-xl border border-stone-200/70 bg-stone-50 px-2 py-1 text-[10px] font-medium text-stone-600 sm:px-3 sm:py-2 sm:text-xs">
                    <span className="hidden sm:inline">剩余额度 </span>{availableQuota}
                  </div>
                  {activeTaskCount > 0 && (
                    <div className="flex shrink-0 items-center gap-1 rounded-xl border border-amber-100 bg-amber-50 px-2 py-1 text-[10px] font-medium text-amber-700 sm:gap-1.5 sm:px-3 sm:py-2 sm:text-xs">
                      <LoaderCircle className="size-3 animate-spin" />
                      {activeTaskCount}<span className="hidden sm:inline"> 个任务处理中</span>
                    </div>
                  )}
                  <div className="relative flex h-9 min-w-0 shrink items-center rounded-full bg-transparent text-[11px] sm:h-auto sm:shrink-0 sm:text-[13px]">
                    <button
                      ref={sizeMenuBtnRef}
                      type="button"
                    className="inline-flex h-9 w-fit max-w-[calc(100vw-12rem)] items-center justify-between gap-2 rounded-xl bg-stone-100 px-4 text-left text-xs font-semibold text-stone-900 sm:h-10 sm:max-w-none sm:text-sm"
                      onClick={() => {
                        if (!isSizeMenuOpen && sizeMenuBtnRef.current) {
                          const rect = sizeMenuBtnRef.current.getBoundingClientRect();
                          const menuWidth = Math.min(460, window.innerWidth - 32);
                          setSizeMenuPos({ top: rect.top - 8, left: Math.max(16, Math.min(rect.left, window.innerWidth - menuWidth - 16)) });
                        }
                        setIsSizeMenuOpen((open) => !open);
                      }}
                    >
                      <span className="truncate">{imageSizeLabel}</span>
                      <ChevronDown className={cn("size-4 shrink-0 opacity-60 transition", isSizeMenuOpen && "rotate-180")} />
                    </button>
                    {isSizeMenuOpen ? (
                      <div
                        ref={sizeMenuRef}
                        className="fixed z-[80] max-h-[62dvh] overflow-y-auto rounded-[24px] border border-stone-200/70 bg-white p-4 shadow-[0_30px_90px_-34px_rgba(15,23,42,0.42)] sm:max-h-none sm:overflow-visible"
                        style={{
                          top: sizeMenuPos.top,
                          left: sizeMenuPos.left,
                          transform: "translateY(-100%)",
                          width: "min(460px, calc(100vw - 2rem))",
                        }}
                      >
                        <h3 className="mb-3 text-base font-semibold text-stone-950">图像设置</h3>
                        <div className="mb-3">
                          <div className="mb-2 text-sm font-medium text-stone-900">账号池</div>
                          <Select
                            value={accountPool}
                            onValueChange={(value) => onAccountPoolChange(value as "default" | "gptfree")}
                          >
                            <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white text-sm shadow-none">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent className="z-[120]">
                              <SelectItem value="default">默认号池</SelectItem>
                              <SelectItem value="gptfree">gptFree号池</SelectItem>
                            </SelectContent>
                          </Select>
                        </div>
                        <div className="mb-3">
                          <div className="mb-2 text-sm font-medium text-stone-900">模型</div>
                          <Select
                            value={imageModel}
                            onValueChange={(value) => {
                              onImageModelChange(value as ImageModel);
                            }}
                          >
                            <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white text-sm shadow-none">
                              <div className="flex min-w-0 items-center gap-2">
                                <img
                                  src="/openai.svg"
                                  alt=""
                                  aria-hidden="true"
                                  className="size-4 shrink-0 text-stone-700"
                                />
                                <span className="truncate">{selectedModelLabel}</span>
                              </div>
                            </SelectTrigger>
                            <SelectContent className="z-[120]">
                              {modelOptions.map((option) => (
                                <SelectItem
                                  key={option.value}
                                  value={option.value}
                                  className="pl-10"
                                  style={{
                                    backgroundImage: "url('/openai.svg')",
                                    backgroundRepeat: "no-repeat",
                                    backgroundPosition: "12px center",
                                    backgroundSize: "16px 16px",
                                  }}
                                >
                                  {option.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                        <div className="mb-3">
                          <div className="mb-2 text-sm font-medium text-stone-900">质量</div>
                          <div className="grid grid-cols-4 gap-2">
                            {qualityOptions.map((option) => {
                              const active = option.value === imageQuality;
                              return (
                                <button
                                  key={option.value}
                                  type="button"
                                  className={cn(
                                    "h-9 cursor-pointer rounded-full border border-stone-200 bg-white text-sm text-stone-800 transition hover:border-stone-300 hover:bg-stone-50",
                                    active && "border-stone-950 bg-white font-medium text-stone-950",
                                  )}
                                  onClick={() => onImageQualityChange(option.value)}
                                >
                                  {option.label}
                                </button>
                              );
                            })}
                          </div>
                        </div>
                        <div className="mb-3">
                          <div className="mb-2 flex items-center gap-1.5 text-sm font-medium text-stone-900">
                            尺寸 <Info className="size-3.5 text-stone-400" />
                          </div>
                          <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-2">
                            <div className="flex items-center rounded-lg bg-stone-100 px-3 py-1.5 text-sm text-stone-700">
                              <span className="mr-2 text-stone-500">W</span>
                              <Input
                                type="number"
                                inputMode="numeric"
                                min="1"
                                value={imageWidth}
                                onChange={(event) => onImageWidthChange(event.target.value)}
                                className="h-7 border-0 bg-transparent px-0 text-sm font-medium text-stone-800 shadow-none focus-visible:ring-0"
                              />
                            </div>
                            <span className="text-stone-400">×</span>
                            <div className="flex items-center rounded-lg bg-stone-100 px-3 py-1.5 text-sm text-stone-700">
                              <span className="mr-2 text-stone-500">H</span>
                              <Input
                                type="number"
                                inputMode="numeric"
                                min="1"
                                value={imageHeight}
                                onChange={(event) => onImageHeightChange(event.target.value)}
                                className="h-7 border-0 bg-transparent px-0 text-sm font-medium text-stone-800 shadow-none focus-visible:ring-0"
                              />
                            </div>
                          </div>
                        </div>
                        <div className="mb-3">
                          <div className="mb-2 flex items-center gap-1.5 text-sm font-medium text-stone-900">
                            宽高比 <Info className="size-3.5 text-stone-400" />
                          </div>
                          <div className="grid grid-cols-4 gap-2 sm:grid-cols-5">
                            {aspectOptions.map((option) => {
                              const active = option.ratio === imageRatio && option.tier === imageTier && option.width === imageWidth && option.height === imageHeight;
                              const Icon = option.icon;
                              const disabled = !isCodexModel && (option.tier === "2k" || option.tier === "4k");
                              return (
                                <button
                                  key={`${option.ratio}-${option.tier}-${option.label}`}
                                  type="button"
                                  disabled={disabled}
                                  className={cn(
                                    "flex h-[64px] cursor-pointer flex-col items-center justify-center gap-1 rounded-2xl border border-stone-200 bg-white text-sm text-stone-800 transition hover:border-stone-300 hover:bg-stone-50",
                                    active && "border-stone-950",
                                    disabled && "cursor-not-allowed border-stone-100 bg-stone-50 text-stone-300 hover:border-stone-100 hover:bg-stone-50",
                                  )}
                                  onClick={() => {
                                    if (disabled) {
                                      return;
                                    }
                                    onImageRatioChange(option.ratio);
                                    onImageTierChange(option.tier);
                                    onImageWidthChange(option.width);
                                    onImageHeightChange(option.height);
                                  }}
                                >
                                  {Icon ? (
                                    <>
                                      <Icon className="size-3.5 stroke-[1.8]" />
                                      <span>{option.label}</span>
                                    </>
                                  ) : (
                                    <span>{option.label}</span>
                                  )}
                                </button>
                              );
                            })}
                          </div>
                        </div>
                        <div className="border-t border-stone-100 pt-3">
                          <div className="mb-2 text-sm font-medium text-stone-900">生成数量</div>
                          <div className="grid grid-cols-4 gap-2 sm:grid-cols-5">
                            {countOptions.map((option) => {
                              const active = imageCount === option;
                              return (
                                <button
                                  key={option}
                                  type="button"
                                  className={cn(
                                    "h-9 cursor-pointer rounded-full border border-stone-200 bg-white text-sm text-stone-800 transition hover:border-stone-300 hover:bg-stone-50",
                                    active && "border-stone-950 bg-white font-medium text-stone-950",
                                  )}
                                  onClick={() => onImageCountChange(option)}
                                >
                                  {option} 张
                                </button>
                              );
                            })}
                            <Input
                              type="number"
                              inputMode="numeric"
                              min="1"
                              max="100"
                              step="1"
                              value={imageCount}
                              onChange={(event) => onImageCountChange(event.target.value)}
                              className="h-9 rounded-full border-stone-200 bg-white px-3 text-center text-sm font-medium text-stone-800 shadow-none focus-visible:ring-0"
                            />
                          </div>
                        </div>
                      </div>
                    ) : null}
                  </div>
                </div>

                <button
                  type="button"
                  onClick={() => void onSubmit()}
                  disabled={!prompt.trim()}
                  className="inline-flex size-10 shrink-0 items-center justify-center rounded-xl bg-stone-950 text-white transition hover:bg-stone-800 disabled:cursor-not-allowed disabled:bg-stone-300 sm:size-11"
                  aria-label={referenceImages.length > 0 ? "编辑图片" : "生成图片"}
                >
                  <ArrowUp className="size-3.5 sm:size-4" />
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
