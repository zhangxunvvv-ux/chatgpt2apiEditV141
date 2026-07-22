"use client";

import webConfig from "@/constants/common-env";
import { getStoredAuthKey } from "@/store/auth";

export type ImageChatConfig = {
  model: string;
  customModel: string;
  reasoningEffort: string;
  accountPool: "default" | "gptfree";
};

export const DEFAULT_CHAT_MODELS = [
  "auto",
  "gpt-5-6-sol",
  "gpt-5-6-Luna",
  "gpt-5-5",
  "gpt-5-5-thinking",
  "gpt-5-4-t-mini",
  "gpt-5-3",
  "gpt-5-3-mini",
  "gpt-5-mini",
];

export function getEffectiveChatModel(config: ImageChatConfig) {
  return config.model === "custom" ? config.customModel.trim() || "auto" : config.model;
}

function streamDeltaFromPayload(payload: unknown) {
  if (!payload || typeof payload !== "object") {
    return "";
  }
  const item = payload as { choices?: Array<{ delta?: { content?: unknown }; message?: { content?: unknown } }> };
  const choice = item.choices?.[0];
  const delta = choice?.delta?.content ?? choice?.message?.content;
  return typeof delta === "string" ? delta : "";
}

export async function streamChatCompletion(
  body: Record<string, unknown>,
  signal: AbortSignal,
  onDelta: (text: string) => void,
) {
  const authKey = await getStoredAuthKey();
  const baseUrl = webConfig.apiUrl.replace(/\/$/, "");
  const response = await fetch(`${baseUrl}/v1/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(authKey ? { Authorization: `Bearer ${authKey}` } : {}),
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = String(payload?.detail?.error || payload?.detail || payload?.error || payload?.message || "");
    } catch {
      detail = await response.text();
    }
    throw new Error(detail || `请求失败 (${response.status})`);
  }

  if (!response.body) {
    throw new Error("浏览器不支持流式响应");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() || "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) {
        continue;
      }
      const data = trimmed.slice(5).trim();
      if (!data || data === "[DONE]") {
        continue;
      }
      try {
        const delta = streamDeltaFromPayload(JSON.parse(data));
        if (delta) {
          onDelta(delta);
        }
      } catch {
        // Ignore malformed heartbeat/debug lines.
      }
    }
  }
}
