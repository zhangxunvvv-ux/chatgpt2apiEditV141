"use client";

import { useEffect, useMemo, useState } from "react";
import { LoaderCircle, Pencil, Plus, Search, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  createPromptLibraryItem,
  deletePromptLibraryItem,
  fetchPromptLibrary,
  updatePromptLibraryItem,
  type PromptLibraryItem,
} from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";

type PromptForm = {
  id?: string;
  name: string;
  type: string;
  content: string;
  note: string;
};

const emptyForm: PromptForm = {
  name: "",
  type: "默认",
  content: "",
  note: "",
};

export default function PromptsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin", "user"]);
  const [items, setItems] = useState<PromptLibraryItem[]>([]);
  const [types, setTypes] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [form, setForm] = useState<PromptForm>(emptyForm);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const data = await fetchPromptLibrary();
      setItems(data.items);
      setTypes(data.types);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "加载提示词失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!session) return;
    void load();
  }, [session]);

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return items.filter((item) => {
      const matchesType = typeFilter === "all" || item.type === typeFilter;
      const haystack = `${item.name}\n${item.type}\n${item.content}\n${item.note || ""}`.toLowerCase();
      return matchesType && (!keyword || haystack.includes(keyword));
    });
  }, [items, query, typeFilter]);

  const resetForm = () => setForm(emptyForm);

  const save = async () => {
    if (!form.content.trim()) {
      toast.error("请填写提示词内容");
      return;
    }
    setSaving(true);
    try {
      const payload = {
        name: form.name.trim() || form.content.trim().slice(0, 24),
        type: form.type.trim() || "默认",
        content: form.content,
        note: form.note,
      };
      const data = form.id
        ? await updatePromptLibraryItem(form.id, payload)
        : await createPromptLibraryItem(payload);
      setItems(data.items);
      setTypes(data.types);
      resetForm();
      toast.success(form.id ? "提示词已更新" : "提示词已保存");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id: string) => {
    if (!window.confirm("确认删除这个提示词吗？")) return;
    try {
      const data = await deletePromptLibraryItem(id);
      setItems(data.items);
      setTypes(data.types);
      if (form.id === id) resetForm();
      toast.success("已删除");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "删除失败");
    }
  };

  if (isCheckingAuth || !session) {
    return (
      <div className="flex h-full items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return (
    <section className="mx-auto flex h-full min-h-0 w-full max-w-6xl flex-col gap-4 overflow-y-auto px-1 py-4 sm:px-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-stone-950">提示词</h1>
          <p className="mt-1 text-sm text-stone-500">保存经常使用的提示词，并在生图时快速选择。</p>
        </div>
        <Button className="rounded-xl bg-stone-950 text-white hover:bg-stone-800" onClick={resetForm}>
          <Plus className="size-4" />
          新建提示词
        </Button>
      </div>

      <div className="grid min-h-0 gap-4 lg:grid-cols-[380px_minmax(0,1fr)]">
        <div className="rounded-2xl border border-stone-200/80 bg-white/85 p-4 shadow-[0_18px_70px_-52px_rgba(15,23,42,0.45)] backdrop-blur">
          <div className="mb-3 text-sm font-semibold text-stone-900">{form.id ? "编辑提示词" : "新增提示词"}</div>
          <div className="space-y-3">
            <Input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="名称" className="rounded-xl border-stone-200 bg-white" />
            <Input value={form.type} onChange={(event) => setForm({ ...form, type: event.target.value })} placeholder="类型，例如：摄影、人像、产品图" className="rounded-xl border-stone-200 bg-white" />
            <Textarea value={form.content} onChange={(event) => setForm({ ...form, content: event.target.value })} placeholder="提示词内容" className="min-h-48 resize-none rounded-xl border-stone-200 bg-white" />
            <Textarea value={form.note} onChange={(event) => setForm({ ...form, note: event.target.value })} placeholder="备注，可选" className="min-h-20 resize-none rounded-xl border-stone-200 bg-white" />
            <div className="flex gap-2">
              <Button className="flex-1 rounded-xl bg-stone-950 text-white hover:bg-stone-800" disabled={saving} onClick={() => void save()}>
                {saving ? <LoaderCircle className="size-4 animate-spin" /> : null}
                保存
              </Button>
              <Button variant="outline" className="rounded-xl border-stone-200 bg-white" onClick={resetForm}>取消</Button>
            </div>
          </div>
        </div>

        <div className="flex min-h-0 flex-col rounded-2xl border border-stone-200/80 bg-white/75 p-4 shadow-[0_18px_70px_-52px_rgba(15,23,42,0.45)] backdrop-blur">
          <div className="mb-3 flex flex-col gap-2 sm:flex-row">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-stone-400" />
              <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、内容或备注" className="rounded-xl border-stone-200 bg-white pl-9" />
            </div>
            <Select value={typeFilter} onValueChange={setTypeFilter}>
              <SelectTrigger className="w-full rounded-xl border-stone-200 bg-white sm:w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部类型</SelectItem>
                {types.map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>

          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
            {loading ? (
              <div className="flex h-40 items-center justify-center"><LoaderCircle className="size-5 animate-spin text-stone-400" /></div>
            ) : filtered.length === 0 ? (
              <div className="flex h-40 items-center justify-center rounded-xl border border-dashed border-stone-200 bg-stone-50 text-sm text-stone-500">暂无提示词</div>
            ) : (
              filtered.map((item) => (
                <div key={item.id} className="rounded-xl border border-stone-200 bg-white p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-semibold text-stone-900">{item.name}</div>
                      <div className="mt-1 inline-flex rounded-lg bg-stone-100 px-2 py-0.5 text-xs text-stone-500">{item.type}</div>
                    </div>
                    <div className="flex shrink-0 gap-1">
                      <Button variant="outline" size="sm" className="size-8 rounded-xl border-stone-200 bg-white p-0" onClick={() => setForm({ id: item.id, name: item.name, type: item.type, content: item.content, note: item.note || "" })}>
                        <Pencil className="size-3.5" />
                      </Button>
                      <Button variant="outline" size="sm" className="size-8 rounded-xl border-stone-200 bg-white p-0 text-rose-600" onClick={() => void remove(item.id)}>
                        <Trash2 className="size-3.5" />
                      </Button>
                    </div>
                  </div>
                  <pre className="mt-3 whitespace-pre-wrap break-words rounded-xl bg-stone-50 p-3 text-xs leading-5 text-stone-700">{item.content}</pre>
                  {item.note ? <div className="mt-2 text-xs text-stone-500">{item.note}</div> : null}
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </section>
  );
}
