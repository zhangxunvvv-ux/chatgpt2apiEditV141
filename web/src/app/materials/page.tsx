"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ImagePlus, LoaderCircle, Pencil, Search, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  deleteMaterialLibraryItem,
  fetchMaterialLibrary,
  updateMaterialLibraryItem,
  uploadMaterialLibraryItem,
  type MaterialLibraryItem,
} from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";

type MaterialForm = {
  id?: string;
  name: string;
  type: string;
  note: string;
};

const emptyForm: MaterialForm = {
  name: "",
  type: "默认",
  note: "",
};

function formatBytes(value: number) {
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(2)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

export default function MaterialsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin", "user"]);
  const [items, setItems] = useState<MaterialLibraryItem[]>([]);
  const [types, setTypes] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [form, setForm] = useState<MaterialForm>(emptyForm);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    setLoading(true);
    try {
      const data = await fetchMaterialLibrary();
      setItems(data.items);
      setTypes(data.types);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "加载素材失败");
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
      const haystack = `${item.name}\n${item.type}\n${item.note || ""}`.toLowerCase();
      return matchesType && (!keyword || haystack.includes(keyword));
    });
  }, [items, query, typeFilter]);

  const resetForm = () => {
    setForm(emptyForm);
    setUploadFile(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const save = async () => {
    setSaving(true);
    try {
      const payload = {
        name: form.name.trim() || uploadFile?.name || "素材图片",
        type: form.type.trim() || "默认",
        note: form.note,
      };
      const data = form.id
        ? await updateMaterialLibraryItem(form.id, payload)
        : await uploadMaterialLibraryItem(assertUploadFile(uploadFile), payload);
      setItems(data.items);
      setTypes(data.types);
      resetForm();
      toast.success(form.id ? "素材已更新" : "素材已上传");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id: string) => {
    if (!window.confirm("确认删除这个素材吗？")) return;
    try {
      const data = await deleteMaterialLibraryItem(id);
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
          <h1 className="text-2xl font-semibold tracking-tight text-stone-950">素材库</h1>
          <p className="mt-1 text-sm text-stone-500">保存常用图片素材，并在生图时快速作为参考图使用。</p>
        </div>
        <Button className="rounded-xl bg-stone-950 text-white hover:bg-stone-800" onClick={resetForm}>
          <ImagePlus className="size-4" />
          新建素材
        </Button>
      </div>

      <div className="grid min-h-0 gap-4 lg:grid-cols-[360px_minmax(0,1fr)]">
        <div className="rounded-2xl border border-stone-200/80 bg-white/85 p-4 shadow-[0_18px_70px_-52px_rgba(15,23,42,0.45)] backdrop-blur">
          <div className="mb-3 text-sm font-semibold text-stone-900">{form.id ? "编辑素材" : "上传素材"}</div>
          <div className="space-y-3">
            {!form.id ? (
              <>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  className="sr-only"
                  onChange={(event) => {
                    const file = event.target.files?.[0] || null;
                    setUploadFile(file);
                    if (file && !form.name) setForm((current) => ({ ...current, name: file.name.replace(/\.[^.]+$/, "") }));
                  }}
                />
                <button
                  type="button"
                  className="flex h-36 w-full flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-stone-300 bg-stone-50 text-sm text-stone-500 transition hover:border-stone-400 hover:bg-stone-100"
                  onClick={() => fileInputRef.current?.click()}
                >
                  <Upload className="size-5" />
                  {uploadFile ? uploadFile.name : "选择图片素材"}
                </button>
              </>
            ) : null}
            <Input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="名称" className="rounded-xl border-stone-200 bg-white" />
            <Input value={form.type} onChange={(event) => setForm({ ...form, type: event.target.value })} placeholder="类型，例如：人物、产品、背景" className="rounded-xl border-stone-200 bg-white" />
            <Textarea value={form.note} onChange={(event) => setForm({ ...form, note: event.target.value })} placeholder="备注，可选" className="min-h-24 resize-none rounded-xl border-stone-200 bg-white" />
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
              <Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、类型或备注" className="rounded-xl border-stone-200 bg-white pl-9" />
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

          <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-y-auto pr-1 sm:grid-cols-2 xl:grid-cols-3">
            {loading ? (
              <div className="col-span-full flex h-40 items-center justify-center"><LoaderCircle className="size-5 animate-spin text-stone-400" /></div>
            ) : filtered.length === 0 ? (
              <div className="col-span-full flex h-40 items-center justify-center rounded-xl border border-dashed border-stone-200 bg-stone-50 text-sm text-stone-500">暂无素材</div>
            ) : (
              filtered.map((item) => (
                <div key={item.id} className="overflow-hidden rounded-xl border border-stone-200 bg-white">
                  <div className="aspect-square bg-stone-100">
                    <img src={item.thumbnail_url || item.url} alt={item.name} className="h-full w-full object-cover" />
                  </div>
                  <div className="space-y-2 p-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate text-sm font-semibold text-stone-900">{item.name}</div>
                        <div className="mt-1 text-xs text-stone-500">{item.type} · {formatBytes(item.size)}</div>
                      </div>
                      <div className="flex shrink-0 gap-1">
                        <Button variant="outline" size="sm" className="size-8 rounded-xl border-stone-200 bg-white p-0" onClick={() => setForm({ id: item.id, name: item.name, type: item.type, note: item.note || "" })}>
                          <Pencil className="size-3.5" />
                        </Button>
                        <Button variant="outline" size="sm" className="size-8 rounded-xl border-stone-200 bg-white p-0 text-rose-600" onClick={() => void remove(item.id)}>
                          <Trash2 className="size-3.5" />
                        </Button>
                      </div>
                    </div>
                    {item.note ? <div className="line-clamp-2 text-xs leading-5 text-stone-500">{item.note}</div> : null}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </section>
  );
}

function assertUploadFile(file: File | null): File {
  if (!file) {
    throw new Error("请先选择图片素材");
  }
  return file;
}
