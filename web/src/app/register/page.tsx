"use client";

import { useEffect, useRef, useState } from "react";
import { LoaderCircle } from "lucide-react";

import webConfig from "@/constants/common-env";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { fetchGptFreeRegisterConfig, fetchNewRegisterConfig, type RegisterConfig } from "@/lib/api";
import { getStoredAuthKey } from "@/store/auth";

import { useSettingsStore } from "../settings/store";
import { RegisterCard } from "./components/register-card";

function RegisterDataController({
  onNewRegister,
  onGptFreeRegister,
}: {
  onNewRegister: (config: RegisterConfig) => void;
  onGptFreeRegister: (config: RegisterConfig) => void;
}) {
  const didLoadRef = useRef(false);
  const loadRegister = useSettingsStore((state) => state.loadRegister);
  const setRegisterConfig = useSettingsStore((state) => state.setRegisterConfig);

  useEffect(() => {
    if (didLoadRef.current) return;
    didLoadRef.current = true;
    void loadRegister();
  }, [loadRegister]);

  useEffect(() => {
    let source: EventSource | null = null;
    let closed = false;
    void getStoredAuthKey().then((token) => {
      if (closed || !token) return;
      const baseUrl = webConfig.apiUrl.replace(/\/$/, "");
      source = new EventSource(`${baseUrl}/api/register/events?token=${encodeURIComponent(token)}`);
      source.onmessage = (event) => {
        setRegisterConfig(JSON.parse(event.data) as RegisterConfig);
      };
    });
    return () => {
      closed = true;
      source?.close();
    };
  }, [setRegisterConfig]);

  useEffect(() => {
    void fetchNewRegisterConfig().then((data) => onNewRegister(data.register));
  }, [onNewRegister]);

  useEffect(() => {
    void fetchGptFreeRegisterConfig().then((data) => onGptFreeRegister(data.register));
  }, [onGptFreeRegister]);

  useEffect(() => {
    let source: EventSource | null = null;
    let closed = false;
    void getStoredAuthKey().then((token) => {
      if (closed || !token) return;
      const baseUrl = webConfig.apiUrl.replace(/\/$/, "");
      source = new EventSource(`${baseUrl}/api/register/new/events?token=${encodeURIComponent(token)}`);
      source.onmessage = (event) => {
        onNewRegister(JSON.parse(event.data) as RegisterConfig);
      };
    });
    return () => {
      closed = true;
      source?.close();
    };
  }, [onNewRegister]);

  useEffect(() => {
    let source: EventSource | null = null;
    let closed = false;
    void getStoredAuthKey().then((token) => {
      if (closed || !token) return;
      const baseUrl = webConfig.apiUrl.replace(/\/$/, "");
      source = new EventSource(`${baseUrl}/api/register/gptfree/events?token=${encodeURIComponent(token)}`);
      source.onmessage = (event) => {
        onGptFreeRegister(JSON.parse(event.data) as RegisterConfig);
      };
    });
    return () => {
      closed = true;
      source?.close();
    };
  }, [onGptFreeRegister]);

  return null;
}

function RegisterPageContent() {
  const [newRegister, setNewRegister] = useState<RegisterConfig | null>(null);
  const [gptFreeRegister, setGptFreeRegister] = useState<RegisterConfig | null>(null);
  return (
    <>
      <RegisterDataController onNewRegister={setNewRegister} onGptFreeRegister={setGptFreeRegister} />
      <section className="mb-2 flex flex-col gap-1 lg:flex-row lg:items-start lg:justify-between">
        <div className="space-y-1">
          <div className="text-xs font-semibold tracking-[0.18em] text-stone-500 uppercase">Register</div>
          <h1 className="text-2xl font-semibold tracking-tight">ChatGPT注册机</h1>
        </div>
      </section>
      <section>
        <RegisterCard
          newRegister={newRegister}
          onNewRegisterChange={setNewRegister}
          gptFreeRegister={gptFreeRegister}
          onGptFreeRegisterChange={setGptFreeRegister}
        />
      </section>
    </>
  );
}

export default function RegisterPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return <RegisterPageContent />;
}
