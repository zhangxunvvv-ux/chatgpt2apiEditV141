"use client";

import { EditableFilePanel } from "./editable-file-panel";

const defaultPrompt = "需要制作一份《2026年Q2 电商运营工作汇报》PPT，用于公司管理层季度会议汇报，整体页数控制在 8 页以内，风格偏商务科技感。重点体现销售增长、用户增长、广告投放效果以及618活动成果，并通过折线图、柱状图、环形图、漏斗图呈现。";

export function PptPanel() {
  return <EditableFilePanel title="PPT生成" kind="ppt" endpoint="/v1/ppt/generations" defaultPrompt={defaultPrompt} />;
}
