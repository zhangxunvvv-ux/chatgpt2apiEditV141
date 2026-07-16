"use client";

import { useEffect, useState } from "react";
import { Activity, CheckCircle2, Clock3, Copy, LoaderCircle, RefreshCw, ShieldAlert, X } from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { fetchAccountDiagnostics, type AccountPoolDiagnostics } from "@/lib/api";
import { copyTextToClipboard } from "@/lib/clipboard";
import { cn } from "@/lib/utils";

const severityMeta = {
  high: { label: "高", className: "border-rose-200 bg-rose-50 text-rose-700" },
  medium: { label: "中", className: "border-amber-200 bg-amber-50 text-amber-700" },
  low: { label: "低", className: "border-stone-200 bg-stone-50 text-stone-600" },
} as const;

function formatRate(value: number | null) {
  return value === null ? "暂无数据" : `${value.toFixed(1)}%`;
}

function formatDuration(value: number) {
  if (value < 1000) return `${value}ms`;
  return `${(value / 1000).toFixed(1)}s`;
}

function buildAiAnalysisText(data: AccountPoolDiagnostics) {
  return [
    "请分析下面的号池与生图异常数据，判断影响成功率的主要原因，并按优先级给出调度、账号维护、并发和网络方面的优化建议。数据已经脱敏，不包含 Token。",
    "",
    "```json",
    JSON.stringify(
      {
        generated_at: data.generated_at,
        pool_summary: data.summary,
        error_categories: data.error_categories,
        abnormal_accounts: data.anomalies,
        recent_image_logs: data.recent_events,
        collection_performance: data.performance,
      },
      null,
      2,
    ),
    "```",
  ].join("\n");
}

export function AccountDiagnosticsPanel({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<AccountPoolDiagnostics | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isCopying, setIsCopying] = useState(false);

  const loadDiagnostics = async () => {
    setIsLoading(true);
    try {
      setData(await fetchAccountDiagnostics(40));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载号池诊断失败");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void loadDiagnostics();
  }, []);

  const copyForAi = async () => {
    if (!data) return;
    setIsCopying(true);
    try {
      await copyTextToClipboard(buildAiAnalysisText(data));
      toast.success("异常分析数据已复制，可直接粘贴给 AI");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "复制异常分析失败");
    } finally {
      setIsCopying(false);
    }
  };

  return (
    <section className="space-y-3">
      <Card className="overflow-hidden rounded-2xl border-amber-200/70 bg-[linear-gradient(135deg,rgba(255,251,235,.96),rgba(255,255,255,.94)_55%,rgba(240,253,250,.9))] shadow-sm">
        <CardContent className="space-y-4 p-4 sm:p-5">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <div className="flex items-center gap-2">
                <Activity className="size-4 text-amber-600" />
                <h2 className="text-base font-semibold text-stone-900">号池日志与异常分析</h2>
                <Badge variant="outline" className="rounded-md border-emerald-200 bg-emerald-50 text-emerald-700">
                  按需分析
                </Badge>
              </div>
              <p className="mt-1 text-xs leading-5 text-stone-500">
                仅在打开或手动刷新时计算；不增加后台定时任务，日志读取限制在最近有限条目。
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                className="rounded-lg border-amber-200 bg-amber-50 text-amber-800 hover:bg-amber-100"
                onClick={() => void copyForAi()}
                disabled={!data || isLoading || isCopying}
              >
                {isCopying ? <LoaderCircle className="size-4 animate-spin" /> : <Copy className="size-4" />}
                一键复制给 AI
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="rounded-lg border-stone-200 bg-white/80"
                onClick={() => void loadDiagnostics()}
                disabled={isLoading}
              >
                <RefreshCw className={cn("size-4", isLoading && "animate-spin")} />
                刷新分析
              </Button>
              <Button variant="ghost" size="icon" className="size-8 rounded-lg" onClick={onClose} title="关闭分析">
                <X className="size-4" />
              </Button>
            </div>
          </div>

          {isLoading && !data ? (
            <div className="flex min-h-36 items-center justify-center gap-2 text-sm text-stone-500">
              <LoaderCircle className="size-4 animate-spin" />
              正在读取号池快照和最近生图日志
            </div>
          ) : data ? (
            <>
              <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                <div className="rounded-xl border border-white/80 bg-white/75 p-3">
                  <div className="text-xs text-stone-500">可用账号</div>
                  <div className="mt-1 text-2xl font-semibold text-emerald-700">
                    {data.summary.available}<span className="text-sm font-normal text-stone-400"> / {data.summary.total}</span>
                  </div>
                </div>
                <div className="rounded-xl border border-white/80 bg-white/75 p-3">
                  <div className="text-xs text-stone-500">累计生图成功率</div>
                  <div className="mt-1 text-2xl font-semibold text-stone-900">{formatRate(data.summary.success_rate)}</div>
                </div>
                <div className="rounded-xl border border-white/80 bg-white/75 p-3">
                  <div className="text-xs text-stone-500">成功 / 失败</div>
                  <div className="mt-1 text-2xl font-semibold text-stone-900">
                    {data.summary.successes}<span className="text-sm font-normal text-stone-400"> / {data.summary.failures}</span>
                  </div>
                </div>
                <div className="rounded-xl border border-white/80 bg-white/75 p-3">
                  <div className="text-xs text-stone-500">当前在途任务</div>
                  <div className={cn("mt-1 text-2xl font-semibold", data.summary.inflight ? "text-amber-600" : "text-stone-900")}>
                    {data.summary.inflight}
                  </div>
                </div>
              </div>

              <div className="grid gap-3 xl:grid-cols-[minmax(0,.9fr)_minmax(0,1.1fr)]">
                <div className="space-y-3">
                  <div className="rounded-xl border border-stone-200/80 bg-white/80 p-3">
                    <div className="mb-2 flex items-center justify-between">
                      <div className="flex items-center gap-2 text-sm font-semibold text-stone-800">
                        <ShieldAlert className="size-4 text-amber-600" />
                        错误分类
                      </div>
                      <span className="text-[11px] text-stone-400">近期 / 已累计跟踪</span>
                    </div>
                    <div className="space-y-2">
                      {data.error_categories.length ? data.error_categories.slice(0, 8).map((item) => (
                        <div key={item.category} className="rounded-lg bg-stone-50/90 px-3 py-2">
                          <div className="flex items-center justify-between gap-3">
                            <span className="text-sm font-medium text-stone-700">{item.label}</span>
                            <span className="text-xs tabular-nums text-stone-500">{item.recent_count} / {item.count}</span>
                          </div>
                          <p className="mt-1 text-xs leading-5 text-stone-500">{item.suggestion}</p>
                        </div>
                      )) : (
                        <div className="flex items-center gap-2 py-6 text-sm text-stone-400">
                          <CheckCircle2 className="size-4 text-emerald-500" />
                          最近没有可归类的生图异常
                        </div>
                      )}
                    </div>
                  </div>

                  <div className="rounded-xl border border-stone-200/80 bg-white/80 p-3">
                    <div className="mb-2 text-sm font-semibold text-stone-800">异常账号 ({data.anomalies.length})</div>
                    <div className="max-h-80 space-y-2 overflow-y-auto pr-1">
                      {data.anomalies.length ? data.anomalies.map((item, index) => {
                        const severity = severityMeta[item.severity];
                        const attempts = item.successes + item.failures;
                        const successRate = attempts ? `${(item.successes / attempts * 100).toFixed(1)}%` : "暂无";
                        return (
                          <div key={`${item.email}-${index}`} className="rounded-lg border border-stone-100 bg-stone-50/80 p-2.5">
                            <div className="flex items-start justify-between gap-2">
                              <div className="min-w-0">
                                <div className="truncate text-sm font-medium text-stone-700" title={item.email}>{item.email}</div>
                                <div className="mt-0.5 text-[11px] text-stone-400">
                                  {item.type} · 成功率 {successRate} · 连败 {item.consecutive_failures}
                                </div>
                              </div>
                              <Badge variant="outline" className={cn("shrink-0 rounded-md", severity.className)}>{severity.label}</Badge>
                            </div>
                            <div className="mt-2 space-y-1 text-xs leading-5 text-stone-600">
                              {item.reasons.map((reason) => <div key={reason}>· {reason}</div>)}
                              {item.last_error ? <div className="line-clamp-2 text-stone-400" title={item.last_error}>最近错误：{item.last_error}</div> : null}
                            </div>
                          </div>
                        );
                      }) : (
                        <div className="py-6 text-center text-sm text-stone-400">当前未发现明显异常账号</div>
                      )}
                    </div>
                  </div>
                </div>

                <div className="rounded-xl border border-stone-200/80 bg-white/80 p-3">
                  <div className="mb-2 flex items-center justify-between">
                    <div className="flex items-center gap-2 text-sm font-semibold text-stone-800">
                      <Clock3 className="size-4 text-stone-500" />
                      最近生图日志
                    </div>
                    <span className="text-[11px] text-stone-400">{data.generated_at} UTC</span>
                  </div>
                  <div className="max-h-[42rem] space-y-2 overflow-y-auto pr-1">
                    {data.recent_events.length ? data.recent_events.map((event, index) => {
                      const failed = event.status === "failed";
                      return (
                        <div key={`${event.time}-${index}`} className="rounded-lg border border-stone-100 bg-stone-50/70 p-2.5">
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="flex min-w-0 items-center gap-2">
                              <span className={cn("size-2 shrink-0 rounded-full", failed ? "bg-rose-500" : "bg-emerald-500")} />
                              <span className="truncate text-sm font-medium text-stone-700">{event.summary || (failed ? "生图失败" : "生图成功")}</span>
                            </div>
                            <span className="text-[11px] tabular-nums text-stone-400">{event.time}</span>
                          </div>
                          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-stone-500">
                            {event.email ? <span className="max-w-64 truncate" title={event.email}>{event.email}</span> : <span>未关联账号</span>}
                            {event.model ? <span>{event.model}</span> : null}
                            <span>{formatDuration(event.duration_ms)}</span>
                          </div>
                          {failed && event.error ? (
                            <div className="mt-1.5 line-clamp-3 text-xs leading-5 text-rose-600/80" title={event.error}>{event.error}</div>
                          ) : null}
                        </div>
                      );
                    }) : (
                      <div className="py-10 text-center text-sm text-stone-400">最近日志中没有生图调用记录</div>
                    )}
                  </div>
                </div>
              </div>
            </>
          ) : null}
        </CardContent>
      </Card>
    </section>
  );
}
