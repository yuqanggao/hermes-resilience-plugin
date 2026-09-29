/**
 * Auto-Resume — 续跑助手状态徽章 + 全量用户可编辑配置面板
 *
 * 位置：$HERMES_HOME/desktop-plugins/resilience/plugin.js
 * 安装：保存后自动热加载；不出现时 ⌘K → "Reload desktop plugins"。
 */

import {
  host, cn, haptic, atom,
  StatusDot, Tip, usePluginI18n, useValue,
  ScrollArea, Separator
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState, useEffect, useCallback } from 'react'

const ID = 'resilience'
const POLL_MS = 10_000          // 状态轮询间隔
const REST_TIMEOUT = 8_000

// ---------------------------------------------------------------------------
// 共享状态
// ---------------------------------------------------------------------------
const $interrupts = atom([])   // 最近中断条目
const $health = atom(null)      // /health 快照
const $panelOpen = atom(false)
let _ctx = null
let _closePanel = null

// 后端不可用时的静默容错
function rest(path, opts = {}) {
  if (!_ctx || typeof _ctx.rest !== 'function') return Promise.reject(new Error('rest unavailable'))
  return _ctx.rest(path, { ...opts, timeoutMs: REST_TIMEOUT })
}

// ---------------------------------------------------------------------------
// 小工具组件
// ---------------------------------------------------------------------------
function SectionTitle({ children }) {
  return jsx('div', {
    className: 'mb-1.5 mt-3 flex items-center gap-1 text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary)',
    children
  })
}

function Field({ label, hint, children }) {
  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2',
        children: [
          jsx('label', { className: 'text-xs font-medium', children: label }),
          hint ? jsx('span', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: hint }) : null
        ]
      }),
      children
    ]
  })
}

function ToggleRow({ label, hint, checked, onChange, disabled }) {
  return jsxs('div', {
    className: 'flex items-center justify-between gap-3 rounded-md px-2 py-1.5',
    children: [
      jsxs('div', {
        className: 'min-w-0',
        children: [
          jsx('div', { className: 'text-xs', children: label }),
          hint ? jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: hint }) : null
        ]
      }),
      jsxs('button', {
        type: 'button',
        role: 'switch',
        'aria-checked': !!checked,
        disabled: !!disabled,
        onClick: () => { if (typeof onChange === 'function' && !disabled) onChange(!checked) },
        className: cn(
          'relative inline-flex h-4 w-7 shrink-0 items-center rounded-full border transition-colors',
          checked ? 'border-(--ui-accent) bg-(--ui-accent)' : 'border-(--ui-stroke-secondary) bg-(--ui-accent-soft)'
        ),
        children: [jsx('span', {
          className: cn(
            'inline-block h-3 w-3 rounded-full bg-background shadow transition-transform',
            checked ? 'translate-x-3.5' : 'translate-x-0.5'
          )
        })]
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// 模型选择器组件（勾选式，带说明和排序）
// ---------------------------------------------------------------------------
function ModelSelector({ models, selectedIds, onSelect, disabled }) {
  const sorted = [...models].sort((a, b) => {
    const ai = selectedIds.indexOf(a.id)
    const bi = selectedIds.indexOf(b.id)
    if (ai >= 0 && bi >= 0) return ai - bi
    if (ai >= 0) return -1
    if (bi >= 0) return 1
    return 0
  })

  const toggleModel = (id) => {
    if (disabled) return
    const newSelected = selectedIds.includes(id)
      ? selectedIds.filter(x => x !== id)
      : [...selectedIds, id]
    onSelect(newSelected)
  }

  const moveUp = (index) => {
    if (index <= 0 || disabled) return
    const newIds = [...selectedIds]
    ;[newIds[index - 1], newIds[index]] = [newIds[index], newIds[index - 1]]
    onSelect(newIds)
  }

  const moveDown = (index) => {
    if (index >= selectedIds.length - 1 || disabled) return
    const newIds = [...selectedIds]
    ;[newIds[index], newIds[index + 1]] = [newIds[index + 1], newIds[index]]
    onSelect(newIds)
  }

  return jsx('div', {
    className: 'space-y-1',
    children: sorted.map((m, idx) => {
      const isSelected = selectedIds.includes(m.id)
      const orderIdx = selectedIds.indexOf(m.id)
      // 构建优先级链标签
      const chainLabel = orderIdx >= 0
        ? `${orderIdx + 1} → ${selectedIds.length === 1 ? '终' : selectedIds.length}`
        : null
      // 格式化上下文长度
      const ctxLabel = m.context_length
        ? `${Math.floor(m.context_length / 1024)}K`
        : null
      return jsxs('div', {
        key: m.id,
        className: cn(
          'flex items-center gap-2 rounded-md border px-2 py-1.5 transition-colors',
          isSelected ? 'border-(--ui-accent) bg-(--ui-accent-soft)' : 'border-(--ui-stroke-secondary)'
        ),
        children: [
          // 优先级序号或链标签
          orderIdx >= 0 ? jsx('span', {
            className: 'flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-(--ui-accent) text-[0.625rem] font-bold text-background',
            children: orderIdx + 1
          }) : jsx('span', { className: 'w-5' }),
          // 复选框
          jsx('button', {
            type: 'button',
            onClick: () => toggleModel(m.id),
            disabled: disabled,
            className: cn(
              'flex h-4 w-4 shrink-0 items-center justify-center rounded border transition-colors',
              isSelected ? 'border-(--ui-accent) bg-(--ui-accent)' : 'border-(--ui-stroke-secondary)'
            ),
            children: isSelected ? jsx('svg', {
              className: 'h-3 w-3 text-background',
              fill: 'none', viewBox: '0 0 24 24', stroke: 'currentColor',
              children: jsx('path', { strokeLinecap: 'round', strokeLinejoin: 'round', strokeWidth: 3, d: 'M5 13l4 4L19 7' })
            }) : null
          }),
          // 模型信息
          jsxs('div', {
            className: 'min-w-0 flex-1',
            children: [
              jsx('div', {
                className: 'flex items-center gap-1.5',
                children: [
                  jsx('span', { className: 'text-xs font-medium truncate', children: m.name || m.model_key }),
                  chainLabel && jsx('span', {
                    className: 'shrink-0 text-[0.5625rem] font-mono text-(--ui-accent) bg-(--ui-accent-soft) px-1 py-0.5 rounded',
                    children: chainLabel
                  }),
                  ctxLabel && jsx('span', {
                    className: 'shrink-0 text-[0.5625rem] text-(--ui-text-tertiary) bg-(--ui-muted) px-1 py-0.5 rounded',
                    children: ctxLabel
                  })
                ]
              }),
              jsx('div', { className: 'text-[0.625rem] text-(--ui-text-tertiary) truncate', children: m.description })
            ]
          }),
          // 价格标签
          m.pricing && jsx('span', {
            className: cn('shrink-0 text-[0.625rem] font-medium px-1.5 py-0.5 rounded',
              m.pricing.toUpperCase().includes('FREE') ? 'bg-green-500/10 text-green-600' : 'bg-amber-500/10 text-amber-600'),
            children: m.pricing
          }),
          // 排序按钮
          orderIdx >= 0 && !disabled ? jsxs('div', {
            className: 'flex gap-0.5 shrink-0',
            children: [
              jsx('button', { type: 'button', onClick: () => moveUp(orderIdx), disabled: orderIdx === 0,
                className: 'h-4 w-4 rounded text-(--ui-text-tertiary) hover:bg-(--ui-accent-soft) disabled:opacity-30', children: '↑' }),
              jsx('button', { type: 'button', onClick: () => moveDown(orderIdx), disabled: orderIdx === selectedIds.length - 1,
                className: 'h-4 w-4 rounded text-(--ui-text-tertiary) hover:bg-(--ui-accent-soft) disabled:opacity-30', children: '↓' })
            ]
          }) : null
        ]
      }, m.id)
    })
  })
}

// ---------------------------------------------------------------------------
// 模型列表兜底数据（当 /models REST 接口不可用时，UI 仍可用）
// 数据来源：E:/Hermes Agent/config.yaml + model_catalog.json 免费模型
// 同步于 2026-09-28。
// ---------------------------------------------------------------------------
const FALLBACK_MODELS = [
  // Omniroute 网关配置（手动配置）
  { id: 'omniroute/auto/best-coding',    name: '最强编程主力·GLM-5.2 [推荐]', description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  { id: 'omniroute/auto/pro-coding',     name: '专业编程·Kimi-K2.6',           description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  { id: 'omniroute/auto/best-reasoning', name: '最强推理·GLM-5.2',            description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  { id: 'omniroute/auto/pro-reasoning',  name: '专业推理·GLM-5.2',            description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  { id: 'omniroute/auto/best-chat',      name: '最强聊天·GLM-5.2',            description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  { id: 'omniroute/auto/best-fast',      name: '最快通用·GLM-5.2',            description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  { id: 'omniroute/auto/best-vision',    name: '视觉·GLM-5.2',                description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  { id: 'omniroute/auto/smart',          name: '综合智能·GLM-5.2',            description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  { id: 'omniroute/auto/claude-sonnet',  name: '仿Claude质量·GLM-5.2',        description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  { id: 'omniroute/free-first',          name: '优先免费路由',                 description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  // Agnes AI（本地中文模型）
  { id: 'agnes/agnes-3.0-flash',        name: 'Agnes 3.0 Flash 🇨🇳',        description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  { id: 'agnes/agnes-2.5-flash',        name: 'Agnes 2.5 Flash 🇨🇳',        description: '长上下文 · 视觉 · 工具调用 · 深度推理', pricing: 'FREE' },
  // OpenRouter 免费模型（含上下文长度）
  { id: 'openrouter/thinkingmachines/inkling:free',       name: 'Inkling:Free',                description: '免费 · 编码', pricing: 'FREE', context_length: 131072 },
  { id: 'openrouter/thinkingmachines/inkling-small:free', name: 'Inkling Small:Free',          description: '免费 · 轻量编码', pricing: 'FREE', context_length: 131072 },
  { id: 'openrouter/poolside/laguna-s-2.1:free',          name: 'Laguna S 2.1:Free',           description: '免费 · 代码补全', pricing: 'FREE', context_length: 32768 },
  { id: 'openrouter/poolside/laguna-xs-2.1:free',         name: 'Laguna Xs 2.1:Free',          description: '免费 · 轻量代码', pricing: 'FREE', context_length: 32768 },
  { id: 'openrouter/nvidia/nemotron-3-super-120b-a12b:free', name: 'Nemotron 3 Super 120B:Free', description: '免费 · NVIDIA旗舰', pricing: 'FREE', context_length: 131072 },
  { id: 'openrouter/nvidia/nemotron-3-ultra-550b-a55b:free', name: 'Nemotron 3 Ultra 550B:Free', description: '免费 · NVIDIA超大规模', pricing: 'FREE', context_length: 131072 },
  { id: 'openrouter/nvidia/nemotron-3.5-lightning:free',  name: 'Nemotron 3.5 Lightning:Free', description: '免费 · 极速模式', pricing: 'FREE', context_length: 131072 },
  { id: 'openrouter/minimax/minimax-m3:free',             name: 'Minimax M3:Free',             description: '免费 · MiniMax旗舰', pricing: 'FREE', context_length: 131072 },
  { id: 'openrouter/z-ai/glm-5.2:free',                  name: 'GLM-5.2:Free',              description: '免费 · 智谱AI', pricing: 'FREE', context_length: 256000 },
]

// ---------------------------------------------------------------------------
// 配置默认值（与 Python DEFAULTS 保持一致；实际预填以后端 /config 为准）
// ---------------------------------------------------------------------------
const FALLBACK_CONFIG = {
  enabled: true,
  api_retry_on_interrupt: true,
  model_rotation_enabled: true,
  model_rotation_on_errors: ['rate_limit', 'timeout', 'connection_error', 'server_error'],
  model_rotation_fallback_order: [],
  model_auto_rollback_enabled: true,
  model_rollback_after_failures: 2,
  auto_screenshot_on_error: true,
  ocr_engine: 'rapidocr',
  auto_approve_enabled: false,
  stall_watchdog_enabled: true,
  stall_detect_seconds: 180,
  stall_max_auto_resumes: 3,
  smart_auto_choice_enabled: true,
  smart_choice_timeout_seconds: 300
}

// ---------------------------------------------------------------------------
// 配置面板（用户可编辑区）
// ---------------------------------------------------------------------------
function ConfigTab() {
  const [cfg, setCfg] = useState(FALLBACK_CONFIG)
  const [loaded, setLoaded] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [allModels, setAllModels] = useState([])
  const [modelsLoaded, setModelsLoaded] = useState(false)

  // 打开面板时从后端拉取真实配置预填 + 可用模型（REST 失败时用内置兜底数据）
  useEffect(() => {
    let cancelled = false
    const cfgP = rest('/config').catch(() => null)
    const modelsP = rest('/models').catch(() => null)
    Promise.all([cfgP, modelsP]).then(([configRes, modelsRes]) => {
      if (cancelled) return
      const resilienceCfg = (configRes && configRes.resilience) || {}
      setCfg({ ...FALLBACK_CONFIG, ...resilienceCfg })
      // 优先用 REST 返回的模型列表；为空则用内置兜底数据
      const serverModels = modelsRes?.models
      setAllModels(Array.isArray(serverModels) && serverModels.length > 0
        ? serverModels
        : FALLBACK_MODELS)
      setModelsLoaded(true)
      setLoaded(true)
    })
    return () => { cancelled = true }
  }, [])

  const patch = (k, v) => { setCfg(c => ({ ...c, [k]: v })); setSaved(false) }

  const parseList = (text, sep) =>
    String(text || '').split(sep).map(s => s.trim()).filter(Boolean)

  // 处理模型勾选变化
  const handleModelSelect = (newSelectedIds) => {
    patch('model_rotation_fallback_order', newSelectedIds)
  }

  // 保存 → PUT /config（白名单热更新 + 落盘），成功后刷新状态
  const save = async () => {
    if (saving) return
    setSaving(true)
    setSaved(false)
    const payload = {
      resilience: {
        enabled: !!cfg.enabled,
        model_rotation_enabled: !!cfg.model_rotation_enabled,
        model_rotation_on_errors: parseList(cfg._errText, ','),
        model_rotation_fallback_order: cfg.model_rotation_fallback_order || [],
        model_auto_rollback_enabled: !!cfg.model_auto_rollback_enabled,
        model_rollback_after_failures: cfg.model_rollback_after_failures || 2,
        auto_screenshot_on_error: !!cfg.auto_screenshot_on_error,
        ocr_engine: cfg.ocr_engine || 'rapidocr',
        api_retry_on_interrupt: !!cfg.api_retry_on_interrupt,
        auto_approve_enabled: !!cfg.auto_approve_enabled,
        stall_watchdog_enabled: !!cfg.stall_watchdog_enabled,
        stall_detect_seconds: Number(cfg.stall_detect_seconds) || 180,
        stall_max_auto_resumes: Number(cfg.stall_max_auto_resumes) || 3,
        smart_auto_choice_enabled: !!cfg.smart_auto_choice_enabled,
        smart_choice_timeout_seconds: Number(cfg.smart_choice_timeout_seconds) || 300,
      }
    }
    try {
      await rest('/config', { method: 'PUT', body: payload })
      await rest('/refresh-status', { method: 'POST', body: {} }).catch(() => {})
      setSaved(true)
      host.notify({ kind: 'success', message: '续跑助手配置已保存并热更新' })
    } catch (e) {
      host.notify({ kind: 'error', message: `保存失败: ${e && e.message ? e.message : e}` })
    } finally {
      setSaving(false)
    }
  }

  // 初始化错误类型文本框
  useEffect(() => {
    if (loaded && cfg._initTexts !== true) {
      setCfg(c => ({
        ...c,
        _errText: (c.model_rotation_on_errors || []).join(', '),
        _initTexts: true
      }))
    }
  }, [loaded])

  const selectedIds = cfg.model_rotation_fallback_order || []

  return jsxs('div', {
    className: 'flex h-full flex-col gap-1',
    children: [
      // 顶部说明
      jsxs('div', {
        className: 'rounded-md bg-(--ui-accent-soft) px-2.5 py-1.5 text-[0.6875rem] text-(--ui-text-secondary)',
        children: '所有选项均可调整，保存后立即热更新，无需重启。'
      }),

      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: jsxs('div', {
          className: 'flex flex-col pr-2',
          children: [
            // ── 总开关 ─────────────────────────────
            jsx(ToggleRow, {
              label: '续跑助手（总开关）',
              hint: '关闭后所有自动续跑功能停用；打开后按下方配置运行',
              checked: cfg.enabled !== false,
              onChange: v => patch('enabled', v)
            }),

            jsx(Separator, {}),

            // ── 模型轮换 ─────────────────────────────
            jsx(SectionTitle, { children: '模型轮换（报错自动换模型）' }),
            jsx(ToggleRow, {
              label: '启用模型轮换',
              hint: '命中错误类型时自动切到备用模型',
              checked: cfg.model_rotation_enabled,
              onChange: v => patch('model_rotation_enabled', v)
            }),
            jsx(Field, {
              label: '触发的错误类型',
              hint: '逗号分隔，如 rate_limit, timeout',
              children: jsx('input', {
                value: cfg._errText || '',
                onChange: e => patch('_errText', e.target.value),
                placeholder: 'rate_limit, timeout, connection_error, server_error',
                className: 'w-full rounded border border-(--ui-stroke-secondary) bg-background px-2 py-1 text-xs'
              })
            }),
            jsx(ToggleRow, {
              label: '自动回滚到原模型',
              hint: '备选模型连续失败后自动切回原模型',
              checked: cfg.model_auto_rollback_enabled !== false,
              onChange: v => patch('model_auto_rollback_enabled', v)
            }),
            jsx(Field, {
              label: '连续失败次数阈值',
              hint: '备选模型连续失败此次数后自动回滚',
              children: jsx('input', {
                type: 'number',
                min: 1,
                max: 10,
                value: cfg.model_rollback_after_failures || 2,
                onChange: e => patch('model_rollback_after_failures', parseInt(e.target.value, 10) || 2),
                className: 'w-20 rounded border border-(--ui-stroke-secondary) bg-background px-2 py-1 text-xs'
              })
            }),
            jsx(Field, {
              label: '备用模型选择',
              hint: '勾选并按 ↑↓ 调整优先级顺序',
              children: jsxs('div', {
                className: 'flex flex-col gap-2',
                children: [
                  jsxs('div', {
                    className: 'flex items-center justify-end gap-1',
                    children: [
                      jsx('button', {
                        onClick: () => {
                          // 重新从后端拉取模型列表
                          setModelsLoaded(false)
                          rest('/models').then(res => {
                            if (res?.models && res.models.length > 0) {
                              setAllModels(res.models)
                            } else {
                              setAllModels(FALLBACK_MODELS)
                            }
                            setModelsLoaded(true)
                            host.notify({ kind: 'success', message: '模型列表已刷新' })
                          }).catch(() => {
                            setAllModels(FALLBACK_MODELS)
                            setModelsLoaded(true)
                            host.notify({ kind: 'error', message: '刷新失败，使用本地缓存' })
                          })
                        },
                        className: 'rounded px-1.5 py-0.5 text-[0.625rem] bg-(--ui-accent-soft) hover:bg-(--ui-accent) hover:text-background transition-colors',
                        children: '🔄 刷新'
                      })
                    ]
                  }),
                  modelsLoaded
                    ? (allModels && allModels.length > 0)
                      ? jsx(ModelSelector, {
                          models: allModels,
                          selectedIds: selectedIds,
                          onSelect: handleModelSelect,
                          disabled: !cfg.model_rotation_enabled
                        })
                      : jsx('div', {
                          className: 'rounded-md border border-dashed border-(--ui-stroke-secondary) bg-(--ui-accent-soft) px-2 py-2 text-[0.6875rem] text-(--ui-text-tertiary)',
                          children: '后端未返回模型列表'
                        })
                    : jsx('div', {
                        className: 'rounded-md border border-dashed border-(--ui-stroke-secondary) bg-background px-2 py-2 text-[0.6875rem] text-(--ui-text-tertiary)',
                        children: '加载中...'
                      })
                ]
              })
            }),
            jsx(Separator, {}),
            // ── 截图与 OCR ───────────────────────────
            jsx(SectionTitle, { children: '报错截图与自动分析' }),
            jsx(ToggleRow, {
              label: '工具报错时自动截图',
              hint: '高影响工具失败时抓取屏幕供分析',
              checked: cfg.auto_screenshot_on_error,
              onChange: v => patch('auto_screenshot_on_error', v)
            }),
            jsx(Field, {
              label: 'OCR 引擎',
              hint: '截图文字识别方式',
              children: jsx('select', {
                value: cfg.ocr_engine || 'rapidocr',
                onChange: e => patch('ocr_engine', e.target.value),
                className: 'w-52 rounded border border-(--ui-stroke-secondary) bg-background px-2 py-1 text-xs',
                children: [
                  jsx('option', { value: 'rapidocr', children: 'RapidOCR（本地离线，推荐）' }),
                  jsx('option', { value: 'tesseract', children: 'Tesseract（需安装）' }),
                  jsx('option', { value: 'off', children: '关闭 OCR（仅记录截图路径）' })
                ]
              })
            }),

            jsx(Separator, {}),

            // ── 自动审批 ─────────────────────────────
            jsx(SectionTitle, { children: '自动审批' }),
            jsx(ToggleRow, {
              label: '自动审批（总开关）',
              hint: '开启后所有审批弹窗自动通过，无需手动确认',
              checked: cfg.auto_approve_enabled,
              onChange: v => patch('auto_approve_enabled', v)
            }),

            jsx(Separator, {}),

            // ── 停滞看门狗 + 自动续跑（v2.8） ─────────────
            jsx(SectionTitle, { children: '停滞看门狗（自动续跑）' }),
            jsx(ToggleRow, {
              label: '停滞检测（总开关）',
              hint: '项目停下且无正向输出时，自动截图确认并发送续跑指令',
              checked: cfg.stall_watchdog_enabled,
              onChange: v => patch('stall_watchdog_enabled', v)
            }),
            jsx(Field, {
              label: '停滞判定阈值（秒）',
              hint: '无 agent 活动多久后判定为停滞',
              children: jsx('input', {
                type: 'number',
                min: 30,
                step: 30,
                value: cfg.stall_detect_seconds || 180,
                onChange: e => patch('stall_detect_seconds', parseInt(e.target.value, 10) || 180),
                className: 'w-28 rounded border border-(--ui-stroke-secondary) bg-background px-2 py-1 text-xs'
              })
            }),
            jsx(Field, {
              label: '单会话最多自动续跑次数',
              hint: '达到上限后停止续跑，防止死循环',
              children: jsx('input', {
                type: 'number',
                min: 1,
                max: 10,
                step: 1,
                value: cfg.stall_max_auto_resumes || 3,
                onChange: e => patch('stall_max_auto_resumes', parseInt(e.target.value, 10) || 3),
                className: 'w-28 rounded border border-(--ui-stroke-secondary) bg-background px-2 py-1 text-xs'
              })
            }),

            jsx(Separator, {}),

            // ── 智能选项（v2.8） ──────────────────────────
            jsx(SectionTitle, { children: '智能选项（自动选推荐项）' }),
            jsx(ToggleRow, {
              label: '智能选项（总开关）',
              hint: '助手问选择题时，超时无响应自动按推荐项继续执行',
              checked: cfg.smart_auto_choice_enabled,
              onChange: v => patch('smart_auto_choice_enabled', v)
            }),
            jsx(Field, {
              label: '等待超时（秒）',
              hint: '选择题等待多少秒无人响应后自动选推荐项',
              children: jsx('input', {
                type: 'number',
                min: 30,
                step: 30,
                value: cfg.smart_choice_timeout_seconds || 300,
                onChange: e => patch('smart_choice_timeout_seconds', parseInt(e.target.value, 10) || 300),
                className: 'w-28 rounded border border-(--ui-stroke-secondary) bg-background px-2 py-1 text-xs'
              })
            }),

            jsx(Separator, {}),

            // ── 基础续跑 ─────────────────────────────
            jsx(SectionTitle, { children: '基础续跑' }),
            jsx(ToggleRow, {
              label: 'API 中断自动重试',
              checked: cfg.api_retry_on_interrupt,
              onChange: v => patch('api_retry_on_interrupt', v)
            }),

            // 保存按钮
            jsxs('div', {
              className: 'mt-4 flex items-center gap-2',
              children: [
                jsx('button', {
                  type: 'button',
                  onClick: save,
                  disabled: saving,
                  className: 'rounded-md bg-(--ui-accent) px-4 py-1.5 text-xs font-medium text-background opacity-90 hover:opacity-100 disabled:opacity-50',
                  children: saving ? '保存中…' : '保存并热更新'
                }),
                saved ? jsx('span', {
                  className: 'text-[0.6875rem] text-green-600',
                  children: '✓ 已生效'
                }) : null
              ]
            })
          ]
        })
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// 状态面板
// ---------------------------------------------------------------------------
function StatusTab() {
  const interrupts = useValue($interrupts)
  const health = useValue($health)

  const resume = async () => {
    haptic('tap')
    try {
      // 优先走后端 REST：/resume-task（服务端执行 continue_session，不依赖 host.request）
      const res = await rest('/resume-task', { method: 'POST', body: {} })
      host.notify({ kind: 'success', message: res?.success ? '已发送恢复指令' : (res?.error || '恢复失败') })
    } catch (e) {
      // 兜底：后端不可用时退回 host.request（若该方法可用）
      try {
        await host.request('tools.dispatch', { tool: 'continue_session', args: { status: 'resume' } })
        host.notify({ kind: 'success', message: '已发送恢复指令（host.request）' })
      } catch (e2) {
        host.notify({ kind: 'error', message: `恢复失败: ${e2 && e2.message ? e2.message : e2}` })
      }
    }
  }

  const fallbacks = (health && health.model_rotation && health.model_rotation.fallback_order) || []

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2',
    children: [
      // 模型轮换
      jsxs('div', {
        className: 'rounded-md border border-(--ui-stroke-secondary) px-2.5 py-2',
        children: [
          jsx('div', {
            className: 'mb-1 text-[0.6875rem] font-medium',
            children: '模型轮换'
          }),
          fallbacks.length
            ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-secondary)', children: fallbacks.join(' → ') })
            : jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: '未配置备用模型' })
        ]
      }),

      // 最近中断
      jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-tertiary)', children: '最近中断' }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: jsxs('div', {
          className: 'flex flex-col gap-1.5 pr-1',
          children: (interrupts && interrupts.length)
            ? interrupts.slice(0, 8).map((e, i) =>
              jsxs('div', {
                key: i,
                className: 'rounded-md border border-(--ui-stroke-secondary) p-2 text-xs',
                children: [
                  jsxs('div', {
                    className: 'flex items-center justify-between',
                    children: [
                      jsxs('span', {
                        className: 'font-medium',
                        children: [
                          e.error_type || 'unknown',
                          e.is_resumeable ? '' : '（不可恢复）'
                        ]
                      }),
                      jsx('span', {
                        className: 'text-[0.625rem] text-(--ui-text-quaternary)',
                        children: e.timestamp ? new Date(e.timestamp).toLocaleString() : ''
                      })
                    ]
                  }),
                  e.error_message ? jsx('div', {
                    className: 'mt-0.5 text-[0.6875rem] text-(--ui-text-tertiary)',
                    children: String(e.error_message).slice(0, 100)
                  }) : null,
                  e.ocr_text ? jsx('div', {
                    className: 'mt-1 rounded bg-(--ui-accent-soft) px-2 py-1 text-[0.625rem] text-(--ui-text-secondary)',
                    children: 'OCR: ' + String(e.ocr_text).slice(0, 80)
                  }) : null
                ]
              })
            )
            : [jsx('div', {
                key: 'empty',
                className: 'rounded-md bg-(--ui-accent-soft) p-4 text-center text-xs text-(--ui-text-secondary)',
                children: '✅ 无中断记录，会话运行正常'
              })]
        })
      }),

      // 一键恢复
      jsx('button', {
        type: 'button',
        onClick: resume,
        className: 'rounded-md bg-(--ui-accent) px-3 py-1.5 text-xs font-medium text-background opacity-90 hover:opacity-100',
        children: '▶ 恢复任务（continue_session）'
      })
    ]
  })
}

// ---------------------------------------------------------------------------
// 日志面板
// ---------------------------------------------------------------------------
function LogTab() {
  const interrupts = useValue($interrupts)
  const log = interrupts || []
  return jsx(ScrollArea, {
    className: 'h-full',
    children: jsx('div', {
      className: 'flex flex-col gap-1.5 pr-1',
      children: log.length
        ? log.slice(0, 20).map((e, i) =>
          jsxs('div', {
            key: i,
            className: 'rounded-md border border-(--ui-stroke-secondary) p-2',
            children: [
              jsxs('div', { className: 'flex justify-between text-[0.6875rem]', children: [
                jsx('span', { className: 'font-medium', children: (e.action || e.error_type || 'event') }),
                jsx('span', { className: 'text-(--ui-text-quaternary)', children: e.timestamp ? new Date(e.timestamp).toLocaleString() : '' })
              ]}),
              e.screenshot_path ? jsx('div', { className: 'mt-0.5 break-all text-[0.625rem] text-(--ui-text-tertiary)', children: '📷 ' + e.screenshot_path }) : null,
              e.ocr_status ? jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: 'OCR: ' + e.ocr_status }) : null
            ]
          })
        )
        : [jsx('div', { key: 'empty', className: 'py-4 text-center text-xs text-(--ui-text-quaternary)', children: '暂无日志' })]
    })
  })
}

// ---------------------------------------------------------------------------
// 面板容器（Tab 切换）
// ---------------------------------------------------------------------------
function ResiliencePanel() {
  const [tab, setTab] = useState('status')
  const tabBtn = (id, label) => jsx('button', {
    type: 'button',
    onClick: () => setTab(id),
    className: cn(
      'flex-1 rounded px-2 py-1 text-xs transition-colors',
      tab === id ? 'bg-(--ui-accent) text-background' : 'text-(--ui-text-secondary) hover:bg-(--ui-accent-soft)'
    ),
    children: label
  })
  return jsxs('div', {
    className: 'flex h-full flex-col',
    children: [
      jsxs('div', {
        className: 'flex gap-1 border-b border-(--ui-stroke-secondary) px-3 py-2',
        children: [tabBtn('status', '状态'), tabBtn('config', '配置'), tabBtn('log', '日志')]
      }),
      jsx('div', { className: 'min-h-0 flex-1 overflow-y-auto p-3', children: tab === 'status' ? jsx(StatusTab, {}) : tab === 'config' ? jsx(ConfigTab, {}) : jsx(LogTab, {}) })
    ]
  })
}

// ---------------------------------------------------------------------------
// 主徽章（状态栏）
// ---------------------------------------------------------------------------
function statusColor(interrupts) {
  if (!interrupts || interrupts.length === 0) return 'green'
  const latest = interrupts[0]
  const age = Date.now() - new Date(latest.timestamp).getTime()
  return age < 5 * 60 * 1000 ? 'red' : 'amber'
}

function ResilienceChip() {
  const t = usePluginI18n(ID)
  const interrupts = useValue($interrupts)
  const health = useValue($health)
  const open = useValue($panelOpen)

  // 轮询 /health
  const refresh = useCallback(() => {
    rest('/health').then(data => {
      $interrupts.set((data && data.recent) || [])
      $health.set(data || null)
    }).catch(() => { /* 后端不可用 → 静默 */ })
  }, [])

  useEffect(() => {
    refresh()
    const iv = setInterval(refresh, POLL_MS)
    return () => clearInterval(iv)
  }, [refresh])

  const openPanel = () => {
    haptic('tap')
    $panelOpen.set(true)
    if (typeof host.openWorkspace === 'function') {
      _closePanel = host.openWorkspace('resilience-panel', {
        title: '续跑助手',
        minWidth: '24rem',
        dock: { pane: 'workspace', pos: 'right' },
        render: () => jsx(ResiliencePanel, {})
      })
    }
  }

  const closePanel = () => {
    $panelOpen.set(false)
    if (_closePanel && typeof _closePanel === 'function') _closePanel()
  }

  const color = statusColor(interrupts)
  const count = (interrupts || []).length

  return jsx(Tip, {
    label: open ? '' : `续跑助手 · 最近 ${count} 次中断 · 点击配置`,
    children: jsx('button', {
      className: cn(
        'inline-flex h-full items-center gap-1.5 rounded-md px-2 py-1 text-[0.6875rem] transition-colors',
        open ? 'bg-(--ui-accent-soft)' : 'hover:bg-(--ui-accent-soft)',
        'text-(--ui-text-secondary)'
      ),
      'data-state': open ? 'open' : 'closed',
      onClick: open ? closePanel : openPanel,
      children: [
        jsx(StatusDot, {
          tone: color === 'green' ? 'good' : color === 'amber' ? 'warn' : 'bad',
          className: 'size-2'
        }),
        jsx('span', { children: t('label') })
      ]
    })
  })
}

// ---------------------------------------------------------------------------
// 注册
// ---------------------------------------------------------------------------
export default {
  id: ID,
  name: 'Resilience',
  description: '续跑助手：睡眠模式、模型轮换、报错截图 OCR、全量用户可编辑配置',
  register(ctx) {
    _ctx = ctx

    ctx.i18n.register({
      en: { label: 'Auto-Resume' },
      zh: { label: '续跑助手' }
    })

    ctx.register({
      id: ID + '-chip',
      area: 'statusBar.right',
      order: 200,
      render: () => jsx(ResilienceChip, {})
    })
  }
}
