"use client";

import localforage from "localforage";

export type EditableFileDraft = {
  prompt?: string;
  images?: string[];
  title?: string;
};

const editableFileStorage = localforage.createInstance({
  name: "chatgpt2api",
  storeName: "editable_file_history",
});

const draftsKey = (kind: string) => `drafts:${kind}`;
const deletedKey = (kind: string) => `deleted:${kind}`;

export async function listEditableFileDrafts(kind: string): Promise<Record<string, EditableFileDraft>> {
  return (await editableFileStorage.getItem<Record<string, EditableFileDraft>>(draftsKey(kind))) || {};
}

export async function saveEditableFileDrafts(kind: string, drafts: Record<string, EditableFileDraft>): Promise<void> {
  await editableFileStorage.setItem(draftsKey(kind), drafts);
}

export async function listDeletedEditableFileIds(kind: string): Promise<Set<string>> {
  return new Set((await editableFileStorage.getItem<string[]>(deletedKey(kind))) || []);
}

export async function saveDeletedEditableFileIds(kind: string, ids: Set<string>): Promise<void> {
  await editableFileStorage.setItem(deletedKey(kind), [...ids]);
}
