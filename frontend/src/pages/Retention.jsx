import { useEffect, useState } from 'react'
import { api } from '../api'

const MODES = ['连目录销毁', '只删元数据']
const ACTIONS = ['销毁', '只标过期']

export default function Retention() {
  const [pols, setPols] = useState([])
  const [vaults, setVaults] = useState([])
  const [runs, setRuns] = useState([])
  const [batches, setBatches] = useState([])
  const [ov, setOv] = useState(null)
  const [policyId, setPolicyId] = useState('')
  const [mode, setMode] = useState(MODES[0])
  const [msg, setMsg] = useState('')
  const [preview, setPreview] = useState(null)
  const [replay, setReplay] = useState(null)
  const [form, setForm] = useState({ name: '', max_age_days: 90, action: '销毁', vault_id: '' })

  // 扫描后所有表面同刷：策略表、批次回执、销毁流水、待销毁计数条同取真实库态。
  const reload = () => {
    api.policies().then(p => {
      setPols(p)
      setPolicyId(cur => cur || (p[0] ? String(p[0].id) : ''))
      setForm(f => ({ ...f, vault_id: f.vault_id || (p[0]?.vault_id ?? '') }))
    })
    api.destroys().then(setRuns)
    api.batches().then(setBatches)
    api.overview().then(setOv)
  }
  useEffect(() => { reload(); api.vaults().then(setVaults) }, [])

  const current = pols.find(p => String(p.id) === String(policyId))

  async function doPreview() {
    if (!policyId) return
    setMsg(''); setReplay(null)
    try { setPreview(await api.previewRetention(Number(policyId))) }
    catch (e) { setMsg(e.message) }
  }

  async function run() {
    setMsg(''); setReplay(null)
    try {
      const r = await api.runRetention({ policy_id: Number(policyId), mode })
      setMsg(`批次 ${r.batch_id} 完成 · 挂间【${r.vault_name}】· 命中 ${r.matched_count} · 处理 ${r.affected.length} · 占坑跳过 ${r.skipped.length}`)
      setPreview(null)
      reload()
    } catch (e) { setMsg(e.message) }
  }

  async function doReplay(id) {
    setMsg('')
    try { setReplay(await api.replayBatch(id)) }
    catch (e) { setMsg(e.message) }
  }

  async function createPolicy(e) {
    e.preventDefault(); setMsg('')
    try {
      const r = await api.createPolicy({
        name: form.name, max_age_days: Number(form.max_age_days),
        action: form.action, vault_id: Number(form.vault_id),
      })
      setMsg(`策略已建并挂载，id=${r.id}`)
      setForm({ name: '', max_age_days: 90, action: '销毁', vault_id: form.vault_id })
      reload()
    } catch (e) { setMsg(e.message) }
  }

  return (
    <div className="page">
      <h1>片龄保留</h1>
      <p className="lede">
        架构口径：策略挂载【片库间】，执行只扫该间在册到期分区，作用域恒为整间，他间不受影响；
        命中集合可由挂载谓词重放。系统不提供分区键名单挂载。
      </p>

      <form className="panel form" onSubmit={createPolicy}>
        <h2>策略配置入口 · 挂载到片库间</h2>
        <div className="row">
          <label>策略名<input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} required /></label>
          <label>片龄阈值(天)<input type="number" min="0" value={form.max_age_days} onChange={e => setForm({ ...form, max_age_days: e.target.value })} /></label>
          <label>动作<select value={form.action} onChange={e => setForm({ ...form, action: e.target.value })}>
            {ACTIONS.map(a => <option key={a}>{a}</option>)}
          </select></label>
          <label>挂载片库间<select value={form.vault_id} onChange={e => setForm({ ...form, vault_id: e.target.value })} required>
            <option value="">选择</option>
            {vaults.map(v => <option key={v.id} value={v.id}>{v.name}</option>)}
          </select></label>
          <button type="submit">建策略并挂载</button>
        </div>
      </form>

      <div className="table-wrap">
        <h2>策略表（挂载听片库间）</h2>
        <table>
          <thead><tr><th>策略</th><th>片龄阈值</th><th>动作</th><th>挂载片库间</th><th>作用域</th><th>启用</th></tr></thead>
          <tbody>
            {pols.map(p => (
              <tr key={p.id} className={String(p.id) === String(policyId) ? 'on' : ''}>
                <td>{p.name}</td><td>{p.max_age_days}天</td><td>{p.action}</td>
                <td>{p.vault_name}</td><td>整间</td><td>{p.active ? '是' : '否'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel form row">
        <h2 style={{ width: '100%' }}>保留扫描入口</h2>
        <label>策略<select value={policyId} onChange={e => { setPolicyId(e.target.value); setPreview(null); setReplay(null) }}>
          {pols.map(p => <option key={p.id} value={p.id}>{p.name}（挂 {p.vault_name} · {p.max_age_days}天）</option>)}
        </select></label>
        <label>销毁模式<select value={mode} onChange={e => setMode(e.target.value)}>
          {MODES.map(m => <option key={m}>{m}</option>)}
        </select></label>
        <button type="button" className="ghost" onClick={doPreview}>干跑预览（重放谓词）</button>
        <button type="button" onClick={run}>执行保留</button>
      </div>
      {current && (
        <p className="muted">
          当前勾选策略【{current.name}】仅扫挂间【{current.vault_name}】，他间到期分区一律跳过。
        </p>
      )}
      {msg && <p className="msg">{msg}</p>}

      {ov && (
        <section className="panel">
          <h2>待销毁计数条（与总览同源同刷）</h2>
          <div className="stat-grid">
            <div className="stat"><span>待销毁候选</span><strong>{ov.待销毁候选}</strong></div>
            <div className="stat"><span>已销毁</span><strong>{ov.已销毁}</strong></div>
            <div className="stat"><span>扫描中批次</span><strong>{ov.扫描中批次}</strong></div>
            <div className="stat"><span>中断批次</span><strong>{ov.中断批次}</strong></div>
          </div>
          <ul className="plain">
            {ov.片库间.map(v => (
              <li key={v.id}>
                {v.name} · 在册 {v.在册分区数} ·{' '}
                <strong className={v.待销毁候选 ? 'badge' : ''}>待销毁 {v.待销毁候选}</strong>
              </li>
            ))}
          </ul>
        </section>
      )}

      {preview && (
        <section className="panel">
          <h2>预览回执 · {preview.vault_name}（营业日 {preview.business_day}）</h2>
          <p className="muted">挂载谓词：{preview.predicate}</p>
          <p>将命中 {preview.matched.length} 条 · 占坑跳过 {preview.skipped.length} 条</p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>分区键</th><th>片龄</th><th>温层</th><th>判定</th></tr></thead>
              <tbody>
                {preview.matched.map(x => <tr key={x.partition_id}><td>{x.part_key}</td><td>{x.age_days}天</td><td>{x.tier}</td><td>到期命中</td></tr>)}
                {preview.skipped.map(x => <tr key={x.partition_id}><td>{x.part_key}</td><td>{x.age_days}天</td><td>{x.tier}</td><td>{x.reason}</td></tr>)}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <div className="table-wrap">
        <h2>扫描批次回执（终态可解释）</h2>
        <table>
          <thead><tr><th>批次</th><th>策略</th><th>挂间</th><th>作用域</th><th>营业日</th><th>命中</th><th>处理</th><th>跳过</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            {batches.map(b => (
              <tr key={b.id}>
                <td>{b.id}</td><td>{b.policy_name}</td><td>{b.vault_name}</td><td>{b.scope}</td>
                <td>{b.business_day}</td><td>{b.matched_count}</td><td>{b.acted_count}</td><td>{b.skipped_count}</td>
                <td>{b.status}</td>
                <td><button type="button" className="ghost" onClick={() => doReplay(b.id)}>重放谓词</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {replay && (
        <section className="panel">
          <h2>批次 {replay.batch_id} 重放对账</h2>
          <p className="muted">{replay.predicate}</p>
          <p>
            重放命中 {replay.replay_hit_count} 条 · 批次状态 {replay.batch_status} · 实际动作 {replay.actual_actions.length} 条
            · 占坑跳过 {replay.batch_skipped?.length ?? 0} 条
          </p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>分区键</th><th>片龄</th><th>当前终态</th><th>批次动作</th></tr></thead>
              <tbody>
                {replay.replay_hits.map(h => {
                  const act = replay.actual_actions.find(a => a.partition_id === h.partition_id)
                  return (
                    <tr key={h.partition_id}>
                      <td>{h.part_key}</td><td>{h.age_days}天</td><td>{h.current_status}</td>
                      <td>{act ? `${act.status}（片龄${act.age_days}天）` : '未处理（占坑/未在册）'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <div className="table-wrap">
        <h2>销毁流水（含作用域列）</h2>
        <table>
          <thead><tr><th>流水</th><th>批次</th><th>分区</th><th>挂间</th><th>策略</th><th>作用域</th><th>模式</th><th>片龄</th><th>状态</th><th>时间</th></tr></thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.id}>
                <td>{r.id}</td><td>{r.batch_id ?? '—'}</td><td>{r.part_key}</td><td>{r.vault_name ?? '—'}</td>
                <td>{r.policy_name}</td><td>{r.scope}</td><td>{r.mode}</td><td>{r.age_days}天</td>
                <td>{r.status}</td><td>{r.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
