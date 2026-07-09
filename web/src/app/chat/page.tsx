"use client";

import { useCallback, useEffect, useState } from "react";
import { LoaderCircle } from "lucide-react";

import { ImageChatPanel } from "@/app/image/components/image-chat-panel";
import type { ImageChatConfig } from "@/app/image/lib/chat-completions";
import { useAuthGuard } from "@/lib/use-auth-guard";

const CHAT_MODEL_STORAGE_KEY = "chatgpt2api:image_chat_model";
const CHAT_CUSTOM_MODEL_STORAGE_KEY = "chatgpt2api:image_chat_custom_model";
const CHAT_REASONING_STORAGE_KEY = "chatgpt2api:image_chat_reasoning";

function ChatPageContent() {
  const [chatConfig, setChatConfig] = useState<ImageChatConfig>({
    model: "auto",
    customModel: "",
    reasoningEffort: "default",
  });

  useEffect(() => {
    try {
      setChatConfig({
        model: window.localStorage.getItem(CHAT_MODEL_STORAGE_KEY) || "auto",
        customModel: window.localStorage.getItem(CHAT_CUSTOM_MODEL_STORAGE_KEY) || "",
        reasoningEffort: window.localStorage.getItem(CHAT_REASONING_STORAGE_KEY) || "default",
      });
    } catch {
      // localStorage may be unavailable.
    }
  }, []);

  const updateChatConfig = useCallback((patch: Partial<ImageChatConfig>) => {
    setChatConfig((current) => {
      const next = { ...current, ...patch };
      try {
        window.localStorage.setItem(CHAT_MODEL_STORAGE_KEY, next.model);
        window.localStorage.setItem(CHAT_CUSTOM_MODEL_STORAGE_KEY, next.customModel);
        window.localStorage.setItem(CHAT_REASONING_STORAGE_KEY, next.reasoningEffort);
      } catch {
        // localStorage may be full or unavailable.
      }
      return next;
    });
  }, []);

  return (
    <section className="mx-auto flex h-full min-h-0 w-full max-w-5xl flex-col p-3 sm:p-5">
      <ImageChatPanel
        config={chatConfig}
        collapsed={false}
        layout="page"
        onConfigChange={updateChatConfig}
        onCollapsedChange={() => undefined}
      />
    </section>
  );
}

export default function ChatPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin", "user"]);

  if (isCheckingAuth || !session) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return <ChatPageContent />;
}
