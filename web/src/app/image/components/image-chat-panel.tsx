"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Copy, FileText, ImagePlus, LoaderCircle, MessageSquareText, Paperclip, RotateCcw, Send, Square, Trash2, X } from "lucide-react";
import { toast } from "sonner";

import { ImageLightbox } from "@/components/image-lightbox";
import { Button } from "@/components/ui/button";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  DEFAULT_CHAT_MODELS,
  getEffectiveChatModel,
  streamChatCompletion,
  type ImageChatConfig,
} from "@/app/image/lib/chat-completions";
import { fetchModels } from "@/lib/api";
import { cn } from "@/lib/utils";

type ChatRole = "user" | "assistant";

type ChatContentPart =
  | { type: "text"; text: string }
  | { type: "image_url"; image_url: { url: string } };

type ChatAttachment = {
  id: string;
  name: string;
  type: string;
  size: number;
  kind: "image" | "text";
  dataUrl?: string;
  text?: string;
};

type ChatMessage = {
  id: string;
  role: ChatRole;
  text: string;
  attachments?: ChatAttachment[];
};

const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_TEXT_BYTES = 2 * 1024 * 1024;

type ImageChatPanelProps = {
  config: ImageChatConfig;
  collapsed: boolean;
  mobileVisible?: boolean;
  layout?: "sidebar" | "page";
  onConfigChange: (patch: Partial<ImageChatConfig>) => void;
  onCollapsedChange: (collapsed: boolean) => void;
};

function createId() {
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function formatBytes(value: number) {
  if (value >= 1024 * 1024) {
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  }
  if (value >= 1024) {
    return `${Math.round(value / 1024)} KB`;
  }
  return `${value} B`;
}

function isTextLike(file: File) {
  if (file.type.startsWith("text/")) {
    return true;
  }
  return /\.(txt|md|markdown|json|csv|tsv|xml|yaml|yml|log|py|js|ts|tsx|jsx|css|html)$/i.test(file.name);
}

function readAsDataUrl(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error(`${file.name} 读取失败`));
    reader.readAsDataURL(file);
  });
}

function readAsText(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error(`${file.name} 读取失败`));
    reader.readAsText(file);
  });
}

async function readAttachment(file: File): Promise<ChatAttachment> {
  if (file.type.startsWith("image/")) {
    if (file.size > MAX_IMAGE_BYTES) {
      throw new Error(`${file.name} 超过 10MB`);
    }
    return {
      id: createId(),
      name: file.name,
      type: file.type || "image/png",
      size: file.size,
      kind: "image",
      dataUrl: await readAsDataUrl(file),
    };
  }

  if (!isTextLike(file)) {
    throw new Error(`${file.name} 不是可直接阅读的文本文件`);
  }
  if (file.size > MAX_TEXT_BYTES) {
    throw new Error(`${file.name} 超过 2MB`);
  }

  return {
    id: createId(),
    name: file.name,
    type: file.type || "text/plain",
    size: file.size,
    kind: "text",
    text: await readAsText(file),
  };
}

function buildUserContent(text: string, attachments: ChatAttachment[]): string | ChatContentPart[] {
  const textFiles = attachments.filter((item) => item.kind === "text");
  const images = attachments.filter((item) => item.kind === "image" && item.dataUrl);
  const parts: ChatContentPart[] = [];
  const textBlocks = [text.trim()];

  for (const file of textFiles) {
    textBlocks.push(`\n\n[文件: ${file.name}]\n${file.text || ""}`);
  }

  const mergedText = textBlocks.filter(Boolean).join("\n");
  if (mergedText) {
    parts.push({ type: "text", text: mergedText });
  }
  for (const image of images) {
    parts.push({ type: "image_url", image_url: { url: image.dataUrl || "" } });
  }
  return parts.length > 1 || images.length ? parts : mergedText;
}

function buildChatHistory(messages: ChatMessage[]) {
  return messages.map((message) => ({
    role: message.role,
    content: message.text,
  }));
}

export function ImageChatPanel({
  config,
  collapsed,
  mobileVisible = false,
  layout = "sidebar",
  onConfigChange,
  onCollapsedChange,
}: ImageChatPanelProps) {
  const [models, setModels] = useState(DEFAULT_CHAT_MODELS);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState("");
  const [lightboxImages, setLightboxImages] = useState<Array<{ id: string; src: string }>>([]);
  const [lightboxOpen, setLightboxOpen] = useState(false);
  const [lightboxIndex, setLightboxIndex] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const effectiveModel = getEffectiveChatModel(config);
  const canSend = Boolean(input.trim() || attachments.length) && !isSending;
  const isPageLayout = layout === "page";

  useEffect(() => {
    let active = true;
    void fetchModels()
      .then((data) => {
        if (!active) return;
        const textModels = data.data
          .map((item) => String(item.id || "").trim())
          .filter((id) => {
            const normalized = id.toLowerCase();
            return id && !normalized.includes("image") && normalized !== "gptfree" && !normalized.startsWith("gptfree/");
          });
        setModels(Array.from(new Set([...DEFAULT_CHAT_MODELS, ...textModels])));
      })
      .catch(() => {
        if (active) {
          setModels(DEFAULT_CHAT_MODELS);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, isSending]);

  const handleFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    setError("");
    try {
      const next = await Promise.all(Array.from(files).map(readAttachment));
      setAttachments((current) => [...current, ...next].slice(0, 8));
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error(message);
    }
  };

  const stop = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setIsSending(false);
  };

  const openAttachmentLightbox = (items: ChatAttachment[], attachmentId: string) => {
    const images = items
      .filter((item) => item.kind === "image" && item.dataUrl)
      .map((item) => ({ id: item.id, src: item.dataUrl || "" }));
    const index = Math.max(0, images.findIndex((item) => item.id === attachmentId));
    if (images.length === 0) {
      return;
    }
    setLightboxImages(images);
    setLightboxIndex(index);
    setLightboxOpen(true);
  };

  const sendDraft = async (rawText: string, rawAttachments: ChatAttachment[], options: { clearComposer?: boolean } = {}) => {
    if (isSending) {
      return;
    }
    const text = rawText.trim();
    const draftAttachments = rawAttachments.map((item) => ({ ...item }));
    if (!text && draftAttachments.length === 0) {
      return;
    }

    const userMessage: ChatMessage = {
      id: createId(),
      role: "user",
      text,
      attachments: draftAttachments,
    };
    const assistantId = createId();
    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: "assistant",
      text: "",
    };
    const baseMessages = messages;
    const requestMessages = [
      ...buildChatHistory(baseMessages),
      {
        role: "user",
        content: buildUserContent(text, draftAttachments),
      },
    ];

    setMessages([...baseMessages, userMessage, assistantMessage]);
    if (options.clearComposer) {
      setInput("");
      setAttachments([]);
    }
    setError("");
    setIsSending(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamChatCompletion(
        {
          model: effectiveModel,
          messages: requestMessages,
          stream: true,
          account_pool: config.accountPool,
          ...(config.reasoningEffort !== "default" ? { reasoning_effort: config.reasoningEffort } : {}),
        },
        controller.signal,
        (delta) => {
          setMessages((current) =>
            current.map((message) =>
              message.id === assistantId ? { ...message, text: message.text + delta } : message,
            ),
          );
        },
      );
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        setMessages((current) =>
          current.map((message) =>
            message.id === assistantId && !message.text ? { ...message, text: "已停止" } : message,
          ),
        );
      } else {
        const message = err instanceof Error ? err.message : String(err);
        setError(message);
        setMessages((current) =>
          current.map((item) => (item.id === assistantId ? { ...item, text: `请求失败：${message}` } : item)),
        );
      }
    } finally {
      abortRef.current = null;
      setIsSending(false);
    }
  };

  const send = async () => {
    await sendDraft(input, attachments, { clearComposer: true });
  };

  const copyMessage = async (text: string) => {
    await navigator.clipboard.writeText(text);
    toast.success("已复制");
  };

  const clear = () => {
    stop();
    setMessages([]);
    setAttachments([]);
    setInput("");
    setError("");
  };

  if (collapsed) {
    return (
    <aside className={cn("h-full min-h-0 max-h-full flex-col items-center overflow-hidden border-l border-stone-200/70 bg-white/70 px-1.5 py-3 shadow-[0_18px_70px_-52px_rgba(15,23,42,0.45)] backdrop-blur-xl xl:flex dark:border-white/10 dark:bg-stone-950/55", mobileVisible ? "flex" : "hidden")}>
        <button
          type="button"
          className="inline-flex size-9 items-center justify-center rounded-xl border border-stone-200 bg-white text-stone-600 shadow-sm transition hover:border-stone-300 hover:text-stone-950"
          onClick={() => onCollapsedChange(false)}
          aria-label="展开文字聊天"
          title="展开文字聊天"
        >
          <ChevronLeft className="size-4" />
        </button>
        <button
          type="button"
          className="mt-2 inline-flex size-9 items-center justify-center rounded-xl bg-stone-100 text-stone-600 transition hover:bg-stone-200 hover:text-stone-950"
          onClick={() => onCollapsedChange(false)}
          aria-label="文字聊天"
          title="文字聊天"
        >
          <MessageSquareText className="size-4" />
        </button>
        {isSending ? <span className="mt-3 size-2 rounded-full bg-emerald-500" title="正在回复" /> : null}
      </aside>
    );
  }

  return (
    <aside
      className={cn(
        "h-full min-h-0 max-h-full flex-col overflow-hidden border-stone-200/70 bg-white/70 px-3 pt-3 shadow-[0_18px_70px_-52px_rgba(15,23,42,0.45)] backdrop-blur-xl dark:border-white/10 dark:bg-stone-950/55",
        isPageLayout ? "flex rounded-2xl border p-4" : "xl:flex xl:border-l xl:pl-3 xl:pr-0",
        !isPageLayout && (mobileVisible ? "flex" : "hidden"),
      )}
    >
      <ImageLightbox
        images={lightboxImages}
        currentIndex={lightboxIndex}
        open={lightboxOpen}
        onOpenChange={setLightboxOpen}
        onIndexChange={setLightboxIndex}
      />
      <div className="flex items-center justify-between gap-2 px-1 pb-3">
        <div className="flex min-w-0 items-center gap-2">
          {!isPageLayout ? (
            <button
              type="button"
              className="inline-flex size-8 shrink-0 items-center justify-center rounded-xl border border-stone-200 bg-white text-stone-600 shadow-sm transition hover:border-stone-300 hover:text-stone-950"
              onClick={() => onCollapsedChange(true)}
              aria-label="收起文字聊天"
              title="收起文字聊天"
            >
              <ChevronRight className="size-4" />
            </button>
          ) : null}
          <div className="truncate text-sm font-semibold text-stone-900">文字聊天</div>
        </div>
        <Button variant="outline" size="sm" className="h-8 shrink-0 rounded-xl border-stone-200 bg-white px-2 text-stone-600" onClick={clear}>
          <Trash2 className="size-4" />
        </Button>
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-1 pb-3">
        {messages.length === 0 ? (
          <div className="flex h-full min-h-40 items-center justify-center rounded-xl border border-dashed border-stone-200 bg-stone-50/70 px-4 text-center text-sm text-stone-500">
            这里可以文字聊天，也可以上传图片或文本文件让模型分析。
          </div>
        ) : (
          messages.map((message) => (
            <div
              key={message.id}
              className={cn(
                "group rounded-xl border px-3 py-2.5 text-sm leading-6",
                message.role === "user"
                  ? "border-stone-200 bg-stone-50 text-stone-800"
                  : "border-emerald-100 bg-emerald-50/70 text-stone-800",
              )}
            >
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="text-xs font-medium text-stone-500">{message.role === "user" ? "你" : "GPT"}</span>
                <div className="flex items-center gap-1 opacity-100 transition sm:opacity-0 sm:group-hover:opacity-100">
                  {message.role === "user" ? (
                    <button
                      type="button"
                      className="inline-flex size-6 items-center justify-center rounded-lg text-stone-400 transition hover:bg-white hover:text-stone-800 disabled:cursor-not-allowed disabled:opacity-40"
                      onClick={() => void sendDraft(message.text, message.attachments || [])}
                      disabled={isSending}
                      aria-label="重新发送"
                      title="重新发送"
                    >
                      <RotateCcw className="size-3.5" />
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="inline-flex size-6 items-center justify-center rounded-lg text-stone-400 transition hover:bg-white hover:text-stone-800 disabled:cursor-not-allowed disabled:opacity-40"
                    onClick={() => void copyMessage(message.text)}
                    disabled={!message.text}
                    aria-label="复制消息"
                    title="复制消息"
                  >
                    <Copy className="size-3.5" />
                  </button>
                </div>
              </div>
              {message.attachments?.length ? (
                <div className="mb-2 flex flex-wrap gap-2">
                  {message.attachments.map((item) =>
                    item.kind === "image" && item.dataUrl ? (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => openAttachmentLightbox(message.attachments || [], item.id)}
                        className="h-16 w-16 overflow-hidden rounded-lg border border-stone-200 bg-white transition hover:border-stone-400"
                        aria-label={`放大预览 ${item.name}`}
                      >
                        <img src={item.dataUrl} alt={item.name} className="h-full w-full object-cover" />
                      </button>
                    ) : (
                      <span key={item.id} className="inline-flex max-w-full items-center gap-1 rounded-lg bg-white px-2 py-1 text-xs text-stone-600">
                        <FileText className="size-3" />
                        <span className="truncate">{item.name}</span>
                      </span>
                    ),
                  )}
                </div>
              ) : null}
              <div className="whitespace-pre-wrap break-words">{message.text || (isSending && message.role === "assistant" ? "思考中..." : "")}</div>
            </div>
          ))
        )}
      </div>

      {attachments.length ? (
        <div className="flex max-h-28 flex-wrap gap-2 overflow-y-auto border-t border-stone-200/70 px-1 py-2">
          {attachments.map((item) => (
            <div key={item.id} className="flex max-w-full items-center gap-2 rounded-xl border border-stone-200 bg-white px-2 py-1.5 text-xs text-stone-600">
              {item.kind === "image" && item.dataUrl ? (
                <button
                  type="button"
                  onClick={() => openAttachmentLightbox(attachments, item.id)}
                  className="size-9 overflow-hidden rounded-lg border border-stone-200 bg-stone-50"
                  aria-label={`放大预览 ${item.name}`}
                >
                  <img src={item.dataUrl} alt={item.name} className="h-full w-full object-cover" />
                </button>
              ) : item.kind === "image" ? (
                <ImagePlus className="size-3.5" />
              ) : (
                <FileText className="size-3.5" />
              )}
              <span className="max-w-36 truncate">{item.name}</span>
              <span className="text-stone-400">{formatBytes(item.size)}</span>
              <button type="button" onClick={() => setAttachments((current) => current.filter((file) => file.id !== item.id))} aria-label="移除附件">
                <X className="size-3.5" />
              </button>
            </div>
          ))}
        </div>
      ) : null}

      {error ? <div className="mx-1 mb-2 rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">{error}</div> : null}

      <div className="shrink-0 border-t border-stone-200/70 px-1 pt-3">
        <div className="relative">
          <Textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                void send();
              }
            }}
            placeholder="输入消息，Ctrl/⌘ + Enter 发送"
            className="min-h-24 resize-none rounded-xl border-stone-200 bg-white/95 pb-10 text-sm shadow-sm shadow-stone-200/40 dark:border-white/10 dark:bg-stone-900/80"
          />
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="sr-only"
            accept="image/*,.txt,.md,.json,.csv,.tsv,.xml,.yaml,.yml,.log,.py,.js,.ts,.tsx,.jsx,.css,.html"
            onChange={(event) => {
              void handleFiles(event.target.files);
              event.currentTarget.value = "";
            }}
          />
          <button
            type="button"
            className="absolute bottom-2 left-2 inline-flex size-8 items-center justify-center rounded-xl text-stone-500 transition hover:bg-stone-100 hover:text-stone-800 dark:hover:bg-white/10"
            onClick={() => fileInputRef.current?.click()}
            aria-label="上传"
            title="上传"
          >
            <Paperclip className="size-4" />
          </button>
        </div>
        {config.model === "custom" ? (
          <input
            value={config.customModel}
            onChange={(event) => onConfigChange({ customModel: event.target.value })}
            placeholder="自定义模型 slug"
            className="mt-2 h-8 w-full rounded-lg border border-stone-200 bg-white px-2.5 text-xs text-stone-700 outline-none transition placeholder:text-stone-400 focus:border-stone-300"
          />
        ) : null}
        <div className="mt-2 flex items-center justify-end gap-1.5">
          <div className="flex min-w-0 items-center justify-end gap-1.5">
            <Select value={config.accountPool} onValueChange={(value) => onConfigChange({ accountPool: value as ImageChatConfig["accountPool"] })}>
              <SelectTrigger className="h-9 w-[100px] rounded-xl border-stone-200 bg-white px-2 text-xs shadow-none sm:w-[116px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">默认号池</SelectItem>
                <SelectItem value="gptfree">gptFree号池</SelectItem>
              </SelectContent>
            </Select>
            <Select value={config.model} onValueChange={(value) => onConfigChange({ model: value })}>
              <SelectTrigger className="h-9 w-[104px] rounded-xl border-stone-200 bg-white px-2 text-xs shadow-none sm:w-[128px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {models.map((item) => (
                  <SelectItem key={item} value={item}>{item}</SelectItem>
                ))}
                <SelectItem value="custom">自定义模型</SelectItem>
              </SelectContent>
            </Select>
            <Select value={config.reasoningEffort} onValueChange={(value) => onConfigChange({ reasoningEffort: value })}>
              <SelectTrigger className="h-9 w-[76px] rounded-xl border-stone-200 bg-white px-2 text-xs shadow-none sm:w-[88px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="default">默认</SelectItem>
                <SelectItem value="low">低</SelectItem>
                <SelectItem value="medium">中</SelectItem>
                <SelectItem value="high">高</SelectItem>
                <SelectItem value="xhigh">超高</SelectItem>
              </SelectContent>
            </Select>
            {isSending ? (
              <Button type="button" size="sm" variant="outline" className="h-9 shrink-0 rounded-xl border-stone-200 bg-white px-3 text-stone-700" onClick={stop}>
                <Square className="size-4" />
                停止
              </Button>
            ) : (
              <Button type="button" size="sm" className="h-9 shrink-0 rounded-xl bg-stone-950 px-3 text-white hover:bg-stone-800" disabled={!canSend} onClick={() => void send()}>
                {isSending ? <LoaderCircle className="size-4 animate-spin" /> : <Send className="size-4" />}
                发送
              </Button>
            )}
          </div>
        </div>
      </div>
    </aside>
  );
}
