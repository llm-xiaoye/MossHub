import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Drawer, Input, InputNumber, Select, Space, Spin } from "antd";
import { api, post, persistentPost, savedSubmissions, describeError, type RecordValue } from "./api";
import {ParsingScopeSummary,TechnicalDetails} from "./ParsingPresentation";
import {formatNames} from "./parsingReviewModel";

type Props = {
  document: RecordValue; kb: string; instance: string;
  onClose: () => void; onSubmitted: () => Promise<void>; onSource: () => void;
};
type Options = {
  document_id: string; raw_version_id: string; kind: string; display_name: string;
  sheets: { name: string; state: string; range: string | null }[];
  tables: { sheet: string; name: string; ref: string; header_rows: number }[];
  value_copy_origins: { raw_version_id: string; document_id: string; display_name: string }[];
};
type Preview = {
  document_id: string; raw_version_id: string; config: RecordValue;
  scope: unknown; scope_source: unknown;
};
type Region = {
  id: number; sheet: string; range: string; headerRows: number | null;
  title: string; unit: string; footnotes: string; tableLabel?: string;
};
type JsonTable = { id: number; pointer: string; columns: string };
const TASK_PATH = "/api/parsing/tasks";
const object = (v: unknown): v is RecordValue => v !== null && typeof v === "object" && !Array.isArray(v);
const nonempty = (v: unknown): v is string => typeof v === "string" && v.length > 0;
const lines = (v: string, optional = false) => v.split(/\r?\n/).filter((s) => !optional || s.length > 0);
const tableKey = (t: Options["tables"][number]) => JSON.stringify([t.sheet, t.name, t.ref]);
function pointer(v: string, label: string) {
  if (v !== "" && (!v.startsWith("/") || /~(?:[^01]|$)/.test(v))) throw new Error(label + "不是有效的 JSON Pointer。");
}
function range(v: string, label: string) {
  if (!/^\$?[A-Za-z]{1,3}\$?[1-9][0-9]*(?::\$?[A-Za-z]{1,3}\$?[1-9][0-9]*)?$/.test(v))
    throw new Error(label + "请填写单一 A1 单元格或连续范围。");
}
function ranges(v: string, label: string) {
  const result = lines(v, true); result.forEach((s) => range(s, label)); return result;
}

export default function ParsingConfig({ document, kb, instance, onClose, onSubmitted, onSource }: Props) {
  const documentId = typeof document.document_id === "string" ? document.document_id : "";
  const prefix = "/api/ui/documents/" + encodeURIComponent(documentId);
  const epoch = useRef(0), revision = useRef(0), nextId = useRef(1);
  const [options, setOptions] = useState<Options | null>(null);
  const [loading, setLoading] = useState(true), [previewing, setPreviewing] = useState(false);
  const [submitting, setSubmitting] = useState(false), [error, setError] = useState("");
  const [hold, setHold] = useState(""), [accepted, setAccepted] = useState("");
  const [records, setRecords] = useState(""), [title, setTitle] = useState(""), [texts, setTexts] = useState("");
  const [tables, setTables] = useState<JsonTable[]>([]), [regions, setRegions] = useState<Region[]>([]);
  const [useCopy, setUseCopy] = useState(false), [origin, setOrigin] = useState<string | undefined>();
  const [note, setNote] = useState(""), [preview, setPreview] = useState<Preview | null>(null);
  const [previewInput, setPreviewInput] = useState(""), [checked, setChecked] = useState(false);
  const disabled = loading || previewing || submitting || Boolean(hold) || Boolean(accepted);

  function pending() {
    const items = savedSubmissions().filter((s) => s.path === TASK_PATH && s.instance === instance &&
      s.phase === "AWAITING_CONFIRMATION" && object(s.body) &&
      s.body.kb_id === kb && s.body.document_id === documentId);
    return items.length ? "已有尚未确认的解析提交。原请求键：" + items.map((s) => s.key).join("、") +
      "。请先核对原请求与任务记录，本页不会重复发送。" : "";
  }
  function invalidate() {
    revision.current += 1; setPreview(null); setPreviewInput(""); setChecked(false); setError("");
  }
  useEffect(() => {
    const current = ++epoch.current, controller = new AbortController();
    revision.current += 1; setOptions(null); setLoading(true); setPreviewing(false); setSubmitting(false);
    setError(""); setHold(""); setAccepted(""); setRecords(""); setTitle(""); setTexts("");
    setTables([]); setRegions([]); setUseCopy(false); setOrigin(undefined); setNote("");
    setPreview(null); setPreviewInput(""); setChecked(false);
    async function load() {
      try {
        if (!documentId || !kb || !instance) throw new Error("未取得完整的资料与实例身份。");
        const v = await api<unknown>(prefix + "/parsing-options", { signal: controller.signal }, instance);
        if (!object(v) || v.document_id !== documentId || !nonempty(v.raw_version_id) ||
          !nonempty(v.kind) || typeof v.display_name !== "string" ||
          !Array.isArray(v.sheets) || !Array.isArray(v.tables) || !Array.isArray(v.value_copy_origins))
          throw new Error("解析选项响应不完整或资料身份不一致。");
        if (!v.sheets.every((s: unknown) => object(s) && nonempty(s.name) && typeof s.state === "string" &&
          (s.range === null || typeof s.range === "string")) ||
          !v.tables.every((t: unknown) => object(t) && nonempty(t.sheet) && nonempty(t.name) && nonempty(t.ref) &&
            Number.isInteger(t.header_rows) && t.header_rows >= 0 &&
            v.sheets.some((s: Options["sheets"][number]) => s.name === t.sheet)) ||
          !v.value_copy_origins.every((o: unknown) => object(o) && nonempty(o.raw_version_id) &&
            nonempty(o.document_id) && typeof o.display_name === "string"))
          throw new Error("工作表、Table 或原件选项的响应形状不完整。");
        if (epoch.current === current) { setOptions(v as Options); setHold(pending()); }
      } catch (cause) {
        if (!controller.signal.aborted && epoch.current === current) setError(describeError(cause));
      } finally { if (epoch.current === current) setLoading(false); }
    }
    void load();
    return () => { controller.abort(); epoch.current += 1; };
  }, [documentId, kb, instance]);

  function updateRegion(id: number, change: Partial<Region>) {
    invalidate();
    setRegions((all) => all.map((r) => r.id === id ? { ...r, ...change,
      tableLabel: "sheet" in change || "range" in change ? undefined : r.tableLabel } : r));
  }
  function updateTable(id: number, change: Partial<JsonTable>) {
    invalidate(); setTables((all) => all.map((t) => t.id === id ? { ...t, ...change } : t));
  }
  function addTable(key: string) {
    invalidate(); const t = options?.tables.find((item) => tableKey(item) === key);
    if (!t) { setError("未找到选中的 Excel Table。"); return; }
    if (regions.some((r) => r.sheet === t.sheet && r.range === t.ref)) {
      setError("该工作表范围已添加。"); return;
    }
    const id = nextId.current++;
    setRegions((all) => [...all, { id, sheet: t.sheet, range: t.ref, headerRows: t.header_rows,
      title: "", unit: "", footnotes: "", tableLabel: t.name }]);
  }
  function config(): RecordValue {
    if (!options) throw new Error("解析选项尚未加载。");
    if (options.kind === "json") {
      pointer(records, "记录集合位置："); pointer(title, "标题位置：");
      const textPointers = lines(texts); textPointers.forEach((v) => pointer(v, "正文位置："));
      const json: RecordValue = { records_pointer: records, title_pointer: title, text_pointers: textPointers };
      if (tables.length) json.tables = tables.map((t) => {
        pointer(t.pointer, "表格位置："); const columns = lines(t.columns, true);
        if (!columns.length || new Set(columns).size !== columns.length)
          throw new Error("表格需要非空且不重复的原始列键名。");
        return { pointer: t.pointer, columns };
      });
      return { json };
    }
    if (options.kind === "xlsx") {
      if (!regions.length) throw new Error("请明确添加至少一个 XLSX 处理范围。");
      const xlsx: RecordValue = { regions: regions.map((r) => {
        if (!options.sheets.some((s) => s.name === r.sheet)) throw new Error("请选择实际工作表。");
        range(r.range, "处理范围：");
        if (r.headerRows === null || !Number.isInteger(r.headerRows) || r.headerRows < 0)
          throw new Error("表头行数须为非负整数。");
        return { sheet: r.sheet, range: r.range, header_rows: r.headerRows,
          title: ranges(r.title, "标题："), unit: ranges(r.unit, "单位："), footnotes: ranges(r.footnotes, "脚注：") };
      }) };
      if (useCopy) {
        const selected = options.value_copy_origins.find((o) => o.raw_version_id === origin);
        if (!selected) throw new Error("请显式选择仅值副本所对应的原件。");
        if (!note.trim()) throw new Error("请填写重算与人工核对说明。");
        xlsx.value_copy_of = { raw_version_id: selected.raw_version_id, source: "LOCAL_USER_UI", note };
      }
      return { xlsx };
    }
    return {};
  }
  async function showPreview() {
    if (!options || disabled) return;
    invalidate(); const current = epoch.current, rev = revision.current;
    let requested: RecordValue;
    try { requested = config(); } catch (cause) { setError(describeError(cause)); return; }
    const input = JSON.stringify(requested); setPreviewing(true);
    try {
      const v = await post<unknown>(prefix + "/parsing-preview",
        { expected_raw_version_id: options.raw_version_id, config: requested }, instance);
      if (!object(v) || v.document_id !== options.document_id || v.raw_version_id !== options.raw_version_id ||
        !object(v.config) || v.scope === null || v.scope === undefined ||
        v.scope_source === null || v.scope_source === undefined)
        throw new Error("预览响应不完整或原件版本不一致。");
      if (JSON.stringify(v.config) !== input) throw new Error("预览返回的配置与本次请求不一致，暂不能提交。");
      if (epoch.current === current && revision.current === rev) {
        setPreview(v as Preview); setPreviewInput(input); setChecked(false);
      }
    } catch (cause) { if (epoch.current === current) setError(describeError(cause)); }
    finally { if (epoch.current === current) setPreviewing(false); }
  }
  async function submit() {
    if (!options || !preview || disabled) return;
    const current = epoch.current;
    try {
      const message = pending(); if (message) { setHold(message); return; }
      const input = JSON.stringify(config());
      if (preview.document_id !== options.document_id || preview.raw_version_id !== options.raw_version_id ||
        input !== previewInput || JSON.stringify(preview.config) !== input)
        throw new Error("配置或原件版本已变化，请重新预览。");
    } catch (cause) { setError(describeError(cause)); return; }
    setSubmitting(true); setError(""); let taskId = "";
    try {
      const v = await persistentPost<RecordValue>(TASK_PATH, { kb_id: kb, document_id: options.document_id,
        expected_raw_version_id: options.raw_version_id, config: preview.config }, instance);
      if (!object(v) || !nonempty(v.task_id)) throw new Error("提交响应未提供有效任务编号。");
      taskId = v.task_id; if (epoch.current === current) setAccepted(taskId);
    } catch (cause) {
      if (epoch.current === current) {
        setError(describeError(cause)); let message = "";
        try { message = pending(); } catch { message = "无法读取原提交记录。"; }
        setHold(message || "原提交未得到有效任务确认。本页停止再次发送，请先核对原请求与任务记录。");
      }
    } finally { if (epoch.current === current) setSubmitting(false); }
    if (taskId && epoch.current === current) {
      try { await onSubmitted(); }
      catch (cause) { if (epoch.current === current)
        setError("任务已受理，但资料列表刷新未完成：" + describeError(cause)); }
    }
  }

  return <Drawer open title="选择资料处理范围" width={820} closable={!submitting} maskClosable={!submitting}
    onClose={() => { if (!submitting) onClose(); }}
    extra={<Button disabled={submitting} onClick={onSource}>查看原件</Button>}>
    <div className="stack">
      {error && <Alert type="error" showIcon message={error} />}
      {hold && <Alert type="warning" showIcon message={hold} />}
      {accepted && <Alert type="success" showIcon message="解析任务已受理" description={<span>处理范围已保存。返回资料页即可查看后续进度，有问题时系统会提示。<TechnicalDetails value={{task_id:accepted}} title="查看任务编号"/></span>} />}
      {loading && <Spin tip="正在读取原件范围" />}
      {options && <>
        <div><strong>{options.display_name}</strong><p className="muted">{formatNames[options.kind]??options.kind} · 本次处理已归档的版本</p><div className="parsing-steps"><strong>1 确认处理方式</strong><span>2 开始解析</span><span>3 查看解析结果</span></div><TechnicalDetails value={{document_id:options.document_id,raw_version_id:options.raw_version_id}} title="技术详情：文件版本"/></div>
        {options.kind === "json" && <div className="stack">
          <Alert type="info" showIcon message="记录集合位置相对于全文；其余 Pointer 相对于每条记录。空指针表示根。"
            description="正文指针逐行填写，空行表示记录本身。键名中的斜线和波浪号按 JSON Pointer 规则转义。" />
          <label>记录集合位置<Input disabled={disabled} value={records} placeholder="例如 /records"
            onChange={(e) => { invalidate(); setRecords(e.target.value); }} /></label>
          <label>标题位置<Input disabled={disabled} value={title} placeholder="例如 /title"
            onChange={(e) => { invalidate(); setTitle(e.target.value); }} /></label>
          <label>正文位置，每行一个<Input.TextArea disabled={disabled} rows={3} value={texts} placeholder="/content"
            onChange={(e) => { invalidate(); setTexts(e.target.value); }} /></label>
          {tables.map((t, i) => <section className="surface stack" key={t.id}>
            <strong>{"表格 " + String(i + 1)}</strong>
            <label>表格位置<Input disabled={disabled} value={t.pointer}
              onChange={(e) => updateTable(t.id, { pointer: e.target.value })} /></label>
            <label>原始列键名，每行一个<Input.TextArea disabled={disabled} rows={3} value={t.columns}
              onChange={(e) => updateTable(t.id, { columns: e.target.value })} /></label>
            <span>保留键名原始空格；空白分隔行不作为列。</span>
            <Button disabled={disabled} onClick={() => { invalidate(); setTables((a) => a.filter((v) => v.id !== t.id)); }}>移除表格</Button>
          </section>)}
          <Button disabled={disabled} onClick={() => { invalidate(); const id = nextId.current++;
            setTables((a) => [...a, { id, pointer: "", columns: "" }]); }}>添加表格配置</Button>
        </div>}
        {options.kind === "xlsx" && <div className="stack">
          <Alert type="info" showIcon message="明确选择实际工作表与范围"
            description="标题、单位和脚注使用原件已有范围。公式保留为公式；仅值副本须人工重算、核对并选择对应原件。" />
          <label>从实际 Excel Table 添加范围<Select disabled={disabled || !options.tables.length}
            style={{ width: "100%" }} value={undefined} placeholder={options.tables.length ? "选择 Table" : "没有 Excel Table，请手动添加"}
            options={options.tables.map((t) => ({ value: tableKey(t), label: t.sheet + " / " + t.name + " / " +
              t.ref + " / 表头行数 " + String(t.header_rows) }))} onChange={addTable} /></label>
          {regions.map((r, i) => <section className="surface stack" key={r.id}>
            <strong>{"范围 " + String(i + 1) + (r.tableLabel ? "，来自 Table：" + r.tableLabel : "")}</strong>
            <label>工作表<Select disabled={disabled} style={{ width: "100%" }} value={r.sheet || undefined}
              placeholder="选择实际工作表" options={options.sheets.map((s) => ({ value: s.name,
                label: s.name + " / " + s.state + (s.range ? " / " + s.range : "") }))}
              onChange={(v: string) => updateRegion(r.id, { sheet: v })} /></label>
            <label>A1 处理范围<Input disabled={disabled} value={r.range} placeholder="A1:F20"
              onChange={(e) => updateRegion(r.id, { range: e.target.value })} /></label>
            <label>表头行数<InputNumber disabled={disabled} min={0} precision={0} value={r.headerRows}
              onChange={(v: number | null) => updateRegion(r.id, { headerRows: v })} /></label>
            {([{ key: "title", label: "标题" }, { key: "unit", label: "单位" }, { key: "footnotes", label: "脚注" }] as const)
              .map((f) => <label key={f.key}>{"原件" + f.label + "范围，每行一个，可留空"}
                <Input.TextArea disabled={disabled} rows={2} value={r[f.key]}
                  onChange={(e) => updateRegion(r.id, { [f.key]: e.target.value })} /></label>)}
            <Button disabled={disabled} onClick={() => { invalidate(); setRegions((a) => a.filter((v) => v.id !== r.id)); }}>移除范围</Button>
          </section>)}
          <Button disabled={disabled} onClick={() => { invalidate(); const id = nextId.current++;
            setRegions((a) => [...a, { id, sheet: "", range: "", headerRows: 1, title: "", unit: "", footnotes: "" }]);
          }}>手动添加范围</Button>
          <Checkbox disabled={disabled} checked={useCopy} onChange={(e) => { invalidate(); setUseCopy(e.target.checked); }}>
            当前文件是仅值副本；我已重算并人工核对，需要关联明确原件
          </Checkbox>
          <label>对应原件<Select disabled={disabled || !useCopy} allowClear style={{ width: "100%" }} value={origin}
            placeholder="显式选择原件，不自动选择" options={options.value_copy_origins.map((o) => ({
              value: o.raw_version_id, label: o.display_name + " / " + o.raw_version_id }))}
            onChange={(v: string | undefined) => { invalidate(); setOrigin(v); }} /></label>
          <label>重算与人工核对说明<Input.TextArea disabled={disabled || !useCopy} rows={3} value={note}
            onChange={(e) => { invalidate(); setNote(e.target.value); }} /></label>
        </div>}
        {options.kind !== "json" && options.kind !== "xlsx" && !preview &&
          <Alert type="info" showIcon message={"查看 "+(formatNames[options.kind]??options.kind)+" 的具体处理步骤"} description="点击下方按钮，核对读取范围、文字和图片的处理方式，以及是否需要填写参数。查看说明不会开始解析。" />}
        <Button disabled={disabled} loading={previewing} onClick={() => void showPreview()}>
          {options.kind === "json" || options.kind === "xlsx" ? "查看本次处理范围" : preview ? "重新查看处理说明" : "查看处理说明"}
        </Button>
        {preview && <section className="surface stack">
          <h3>开始前，请核对</h3>
          <ParsingScopeSummary kind={options.kind} config={preview.config} scope={preview.scope} scopeSource={preview.scope_source}/>
          
        </section>}
        <Space><Button type="primary" loading={submitting} disabled={disabled || !preview}
          onClick={() => void submit()}>保存处理范围并继续</Button>
          <Button disabled={submitting} onClick={onClose}>{accepted ? "返回资料管理" : "关闭"}</Button></Space>
      </>}
    </div>
  </Drawer>;
}
