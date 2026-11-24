import React, { useCallback, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, ApiError, statusLabel, type Project, type Summary, type Config, type Me, type Preset } from './api';
import { bindBack, initData, inTelegram, setupPlatform, telegramDownload } from './platform';
import './style.css';

const stepNames = ['Материалы', 'Уточнения', 'План', 'Дизайн', 'Результат'];
const stepIndex: Record<string, number> = { sources: 0, survey: 1, plan: 2, style: 3, background: 3, mode: 3, generating: 4, complete: 4 };

// The fragment never goes to the HTTP server or its access logs. Remove it from
// browser history immediately, then exchange it once (also under StrictMode).
const invitationToken = new URLSearchParams(window.location.hash.slice(1)).get('invite');
if (invitationToken) window.history.replaceState(null, '', window.location.pathname + window.location.search);
let invitationExchange: Promise<unknown> | undefined;

function App() {
  const [config, setConfig] = useState<Config | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [projects, setProjects] = useState<Summary[]>([]);
  const [project, setProject] = useState<Project | null>(null);
  const currentId = useRef<number | null>(null);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [error, setError] = useState('');
  const [working, setWorking] = useState(false);
  const [confirmation, setConfirmation] = useState<'replace' | 'delete' | 'cancel' | null>(null);

  const refreshList = useCallback(async () => {
    const [list, account] = await Promise.all([api<Summary[]>('/projects'), api<Me>('/me')]);
    setProjects(list); setMe(account);
  }, []);
  const showError = useCallback((error: unknown) => {
    setError(error instanceof Error ? error.message : 'Не удалось связаться с сервисом');
    if (error instanceof ApiError && error.status === 401) setMe(null);
  }, []);
  const open = async (id: number) => {
    setError(''); setWorking(true); currentId.current = id;
    try { const p = await api<Project>(`/projects/${id}`); if (currentId.current === id) setProject(p); }
    catch (e) { showError(e); }
    finally { setWorking(false); }
  };
  const home = useCallback(() => { currentId.current = null; setProject(null); setError(''); void refreshList().catch(showError); }, [refreshList, showError]);
  useEffect(() => setupPlatform(), []);
  useEffect(() => bindBack(project ? home : null), [project?.id, home]);
  useEffect(() => {
    void (async () => {
      try {
        setConfig(await api<Config>('/config'));
        if (invitationToken) {
          invitationExchange ??= api('/auth/invite', { method: 'POST', body: JSON.stringify({ token: invitationToken }) });
          await invitationExchange;
        } else if (initData()) await api('/auth/telegram', { method: 'POST', body: JSON.stringify({ init_data: initData() }) });
        try { await refreshList(); setPresets(await api<Preset[]>('/presets')); }
        catch (e) { if (!(e instanceof ApiError && e.status === 401)) throw e; }
      } catch (e) { showError(e); }
      finally { setLoading(false); }
    })();
  }, [refreshList, showError]);
  useEffect(() => {
    if (!project || !me) return;
    const id = project.id;
    const poll = async () => {
      try {
        const p = await api<Project>(`/projects/${id}`);
        if (currentId.current !== id) return;
        setProject(p); setError('');
        if (p.status !== project.status) await refreshList();
      } catch (e) { showError(e); }
    };
    const interval = window.setInterval(() => { if (document.visibilityState === 'visible') void poll(); }, project.busy ? 2500 : 15000);
    const visible = () => { if (document.visibilityState === 'visible') void poll(); };
    document.addEventListener('visibilitychange', visible);
    return () => { window.clearInterval(interval); document.removeEventListener('visibilitychange', visible); };
  }, [project?.id, project?.busy, project?.status, me?.user_id, refreshList, showError]);

  const execute = async (operation: () => Promise<void>) => {
    setWorking(true); setError('');
    try { await operation(); }
    catch (e) {
      showError(e);
      if (e instanceof ApiError && e.status === 409 && currentId.current) {
        setProject(await api<Project>(`/projects/${currentId.current}`).catch(() => project));
      }
    } finally { setWorking(false); }
  };
  const command = async (action: string, values: Record<string, unknown> = {}): Promise<CommandResult> => {
    if (!project) return {};
    let result: CommandResult = {};
    await execute(async () => {
      try {
        const p = await api<Project>(`/projects/${project.id}/commands`, {
          method: 'POST', body: JSON.stringify({ revision: project.revision, action, ...values }),
        });
        result = { project: p };
        setProject(p);
      } catch (error) {
        result = { error: error instanceof Error ? error.message : 'Не удалось сохранить изменения. Повторите попытку.' };
        throw error;
      }
      await refreshList();
    });
    return result;
  };
  const create = async (replace = false) => {
    if (!replace && projects.some(p => p.status === 'in_progress')) { setConfirmation('replace'); return; }
    setConfirmation(null);
    await execute(async () => {
      const p = await api<Project>('/projects', { method: 'POST', body: JSON.stringify({ replace_active: replace }) });
      currentId.current = p.id; setProject(p); await refreshList();
    });
  };
  const upload = async (kind: string, files: FileList | null, number = 1, caption = '') => {
    if (!project || !files?.length) return;
    const saved = Array.from(files);
    await execute(async () => {
      let p = project;
      for (const file of saved) {
        const data = new FormData(); data.append('file', file);
        const query = new URLSearchParams({ kind, revision: String(p.revision), number: String(number), caption });
        p = await api<Project>(`/projects/${p.id}/uploads?${query}`, { method: 'POST', body: data });
        setProject(p);
      }
    });
  };
  const confirm = async () => {
    const action = confirmation; setConfirmation(null);
    if (action === 'replace') await create(true);
    if (action === 'cancel') await command('cancel');
    if (action === 'delete' && project) await execute(async () => {
      await api(`/projects/${project.id}`, { method: 'DELETE' });
      currentId.current = null; setProject(null); await refreshList();
    });
  };
  const active = projects.find(p => p.status === 'in_progress');
  return <div className="app-shell">
    <aside className="sidebar">
      <button className="brand" onClick={home}><span className="brand-icon">P<span>↗</span></span><span>PowerGuide<small>ПРЕЗЕНТАЦИИ С ИИ</small></span></button>
      <div className="side-content"><span className="eyebrow">ВАШЕ РАБОЧЕЕ ПРОСТРАНСТВО</span>
        <h2>От мысли<br/>к убедительным<br/>слайдам.</h2><p>Соберите материалы,<br/>согласуйте план —<br/>остальное подготовит ИИ.</p>
        <div className="side-deck" aria-hidden="true"><div className="deck-back"/><div className="deck-front"><span>POWERGUIDE</span><strong>Идеи, которые<br/>видно.</strong><div className="deck-circle"/><div className="deck-lines"/></div></div>
      </div>
      <div className="pilot"><span className="dot"/> Закрытый пилот <span className="version">v0.1</span></div>
    </aside>
    <main>
      <header className="topbar"><div><span className="mobile-brand">PowerGuide</span><span className="desktop-breadcrumb">Рабочее пространство <span>/</span> {project ? 'Презентация' : 'Мои презентации'}</span></div>
        {me && <div className="account"><span className="avatar">{me.is_dev ? 'Л' : me.is_guest ? 'Т' : 'П'}</span><span>{me.is_dev ? 'Локальный просмотр' : me.is_guest ? me.name || 'Тестовый аккаунт' : 'Участник пилота'}<small>{me.used_today} из {me.daily_limit} запусков сегодня</small></span></div>}
      </header>
      <div className="content">
        {error && <div className="alert error" role="alert">{error}<button aria-label="Закрыть сообщение" onClick={() => setError('')}>×</button></div>}
        {loading ? <div className="empty"><span className="spinner"/><h2>Открываем PowerGuide…</h2></div> : !me ?
          <section className="welcome"><span className="eyebrow">ВАША СЛЕДУЮЩАЯ ПРЕЗЕНТАЦИЯ</span><h1>Начните с идеи.</h1><p className="lead">Превратим описание и материалы в готовую презентацию — шаг за шагом, с вашим участием.</p>
            <div className="card login"><h2>Добро пожаловать</h2><p>Откройте персональную ссылку-приглашение или запустите приложение из Telegram-бота.</p>
              {config?.dev_login && <button className="primary" disabled={working} onClick={() => void execute(async () => {
                await api('/auth/dev', { method: 'POST' }); await refreshList(); setPresets(await api<Preset[]>('/presets'));
              })}>Открыть локальную версию <span>↗</span></button>}
            </div>
          </section> : !project ? <>
          <section className="page-heading"><div><span className="eyebrow">ИДЕЯ → ПЛАН → СЛАЙДЫ</span><h1>Мои презентации</h1><p>Здесь начинается ваша следующая история.</p></div><button className="primary" disabled={working} onClick={() => void create()}>＋ Новая презентация</button></section>
          {active && <button className="active-banner" onClick={() => void open(active.id)}><span className="active-symbol">◷</span><span><small>ПРОДОЛЖИТЬ РАБОТУ</small><strong>{active.title}</strong></span><span>Открыть →</span></button>}
          <div className="section-label"><h2>Все презентации</h2><span>{projects.length}</span></div>
          {!projects.length ? <section className="card empty"><div className="empty-icon">▤</div><h2>Пока чистый лист</h2><p>Расскажите, что хотите донести до аудитории.<br/>Поможем собрать это в презентацию.</p><button className="secondary" onClick={() => void create()}>Создать первую презентацию →</button><div className="feature-row"><span>До 10 слайдов</span><span>Ваши материалы</span><span>PDF и PowerPoint</span></div></section> :
            <div className="project-grid">{projects.map(p => <button className="card project-card" key={p.id} onClick={() => void open(p.id)}>
              <div className={'project-cover cover-'+p.status}><span>POWERGUIDE</span><strong>{p.title}</strong><span className="cover-arrow">↗</span></div>
              <div className="project-info"><span className={'badge '+p.status}>{statusLabel(p.status)}</span><small>{new Date(p.created_at.replace(' ', 'T')+'Z').toLocaleDateString('ru-RU')}</small></div><h3>{p.title}</h3><p>{p.origin === 'chat' ? 'Создана в чате' : 'Создана в приложении'} <span>→</span></p>
            </button>)}</div>}
          </> : <>
            <button className="back-link" onClick={home}>← Все презентации</button>
            <section className="project-heading"><div><span className="eyebrow">ПРЕЗЕНТАЦИЯ #{project.id}</span><h1>{project.title}</h1></div><span className={'badge '+project.status}>{statusLabel(project.status)}</span></section>
            {project.origin === 'miniapp' && <ol className="stepper">{stepNames.map((name, i) => <li key={name} className={i === stepIndex[project.stage] ? 'current' : i < stepIndex[project.stage] ? 'done' : ''}><span>{i < stepIndex[project.stage] ? '✓' : i+1}</span><small>{name}</small></li>)}</ol>}
            {project.busy && <section className="card progress" aria-live="polite"><span className="spinner"/><div><h2>{project.job?.progress || 'Готовим презентацию…'}</h2><p>Можно закрыть приложение и вернуться позже. Прогресс сохраняется.</p></div></section>}
            {!project.busy && project.job && ['failed','interrupted'].includes(project.job.status) && <div className="alert warning"><div><strong>{project.job.status === 'interrupted' ? 'Работа была прервана' : project.job.action === 'deliver' ? 'Не удалось отправить результат в чат' : 'Не удалось завершить шаг'}</strong><p>{project.job.error || 'Сохранённые этапы будут использованы повторно.'}</p></div>
              {project.actions.includes('retry') && <button className="secondary" disabled={working} onClick={() => void command('retry')}>Продолжить</button>}
              {project.actions.includes('deliver') && !me.is_dev && !me.is_guest && <button className="secondary" disabled={working} onClick={() => void command('deliver')}>Отправить ещё раз</button>}
              {project.actions.includes('generate') && project.mode === 'editable' && <button className="secondary" disabled={working} onClick={() => void command('generate',{mode:'standard'})}>Перейти к стандартному режиму</button>}
            </div>}
            <fieldset className="workspace" disabled={working || project.busy}>
              {project.origin === 'chat' && project.status === 'in_progress' ? <section className="card empty"><h2>Продолжите в чате бота</h2><p>Эта презентация начата в Telegram-чате. Здесь появятся её готовые результаты.</p><button className="secondary" onClick={() => void create()}>Создать новую в приложении</button></section> : <>
                {project.stage === 'sources' && <Sources key={project.id} project={project} command={command} upload={upload}/>}
                {project.stage === 'survey' && <Survey key={project.id+':survey'} project={project} command={command} maxSlides={config?.max_slides ?? 10}/>}
                {project.stage === 'plan' && <Plan project={project} command={command} upload={upload}/>}
                {project.stage === 'style' && <section className="card form-card"><span className="eyebrow">ШАГ 4 / ДИЗАЙН</span><h2>Выберите настроение</h2><p>Готовый стиль задаст оформление всех слайдов. Или загрузите свой референс.</p>
                  <div className="preset-grid">{presets.map(p => <button key={p.number} className="preset" onClick={() => void command('select_style',{preset:p.number})}><img src={p.preview} alt={`Пример оформления стиля ${p.number}`}/><span>Стиль {p.number} →</span></button>)}</div>
                  <label className="upload-box">＋ Загрузить референс <small>JPG, PNG или WebP, до 10 МБ</small><input type="file" accept="image/jpeg,image/png,image/webp" onChange={e => { void upload('reference',e.currentTarget.files); e.currentTarget.value=''; }}/></label>
                  {project.reference && <img className="reference" src={project.reference} alt="Ваш референс"/>}
                  <div className="form-actions"><button className="primary" onClick={() => void command('select_style')}>{project.reference ? 'Создать фон по референсу' : 'Подобрать дизайн автоматически'} →</button></div>
                </section>}
                {project.stage === 'background' && <Background project={project} command={command}/>}
                {project.stage === 'mode' && <section className="card form-card"><span className="eyebrow">ВСЁ ГОТОВО К ГЕНЕРАЦИИ</span><h2>В каком формате создадим?</h2><p>План согласован, оформление выбрано. Осталось подготовить слайды.</p><div className="mode-grid">
                  {config?.modes.includes('standard') && <div className="mode-card"><span className="mode-icon">▧</span><h3>Стандартная презентация</h3><p>Слайды-изображения и PDF с художественным оформлением. Дополнительно — простой PPTX для правки текста.</p><button className="primary" onClick={() => void command('generate',{mode:'standard'})}>Создать презентацию →</button></div>}
                  {config?.modes.includes('editable') && <div className="mode-card"><span className="badge experimental">Экспериментальный</span><h3>Редактируемый PowerPoint</h3><p>Текст, фигуры и схемы можно менять в PowerPoint. Фон и иллюстрации остаются изображениями. В комплекте HTML.</p><button className="secondary" onClick={() => void command('generate',{mode:'editable'})}>Создать редактируемую →</button></div>}
                </div><p className="hint">Новые презентации расходуют доступные запуски. Повтор незавершённого этапа использует сохранённый прогресс.</p></section>}
                {project.stage === 'complete' && <section className="card form-card"><span className="eyebrow success-text">ГОТОВО К ВАШИМ ПРАВКАМ И ПОКАЗУ</span><h2>Ваша презентация готова</h2><p>Скачайте результат или просмотрите слайды ниже.</p><Downloads project={project}/>
                  {!!project.previews.length && <div className="slide-previews">{project.previews.map(s => <figure key={s.number}><img src={s.url} alt={`Слайд ${s.number}`} loading="lazy"/><figcaption>Слайд {s.number}</figcaption></figure>)}</div>}
                  {project.html_preview && <div className="html-preview"><iframe title="Предпросмотр презентации" src={project.html_preview} sandbox=""/></div>}
                  {project.text_report && <details><summary>Результат проверки текста</summary><p className="pre-wrap">{project.text_report.replace(/<[^>]*>/g,'')}</p></details>}
                </section>}
                {project.stage === 'cancelled' && <section className="card empty"><h2>Презентация отменена</h2><p>Можно начать новую с другой идеей.</p><button className="primary" onClick={() => void create()}>Новая презентация</button></section>}
              </>}
            </fieldset>
            <div className="project-tools">{project.status === 'in_progress' && project.origin === 'miniapp' && <button className="text-button" disabled={working} onClick={() => setConfirmation('cancel')}>Отменить презентацию</button>}<button className="text-button danger" disabled={working} onClick={() => setConfirmation('delete')}>Удалить проект и файлы</button></div>
          </>}
        <footer>PowerGuide <span>Ваша идея. Убедительная форма.</span></footer>
      </div>
    </main>
    {working && <div className="working" role="status"><span className="spinner"/> Сохраняем…</div>}
    {confirmation && <div className="modal-overlay"><section className="card modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><h2 id="modal-title">{confirmation === 'replace' ? 'Начать новую презентацию?' : confirmation === 'delete' ? 'Удалить презентацию?' : 'Остановить работу?'}</h2><p>{confirmation === 'replace' ? 'Текущая незавершённая презентация будет отменена. Её генерация остановится.' : confirmation === 'delete' ? 'Проект, исходные материалы и готовые файлы будут удалены. Это действие нельзя отменить.' : 'Дальнейшая генерация остановится. Уже отправленный запрос к модели может быть обработан провайдером.'}</p><div className="form-actions"><button className="secondary" onClick={() => setConfirmation(null)}>Назад</button><button className="primary" onClick={() => void confirm()}>{confirmation === 'delete' ? 'Удалить' : confirmation === 'replace' ? 'Создать новую' : 'Остановить'}</button></div></section></div>}
  </div>;
}

type CommandResult = { project?: Project; error?: string };
type Props = { project: Project; command: (action: string, values?: Record<string, unknown>) => Promise<CommandResult> };
type Upload = { upload: (kind: string, files: FileList | null, number?: number, caption?: string) => Promise<void> };

function Sources({project,command,upload}: Props & Upload) {
  const [description,setDescription] = useState(project.description);
  const [ratio,setRatio] = useState(project.answers['design.ratio'] || '16:9');
  const sourceList = project.sources.map(file => file.position).join(',');
  const [confirmedList, setConfirmedList] = useState(project.sources_confirmed ? sourceList : null);
  const [comments, setComments] = useState<Record<number, string>>({});
  const confirmed = confirmedList === sourceList;
  const analyzeDisabled = !description.trim() || (!!sourceList && !confirmed);
  const values = {
    description, ratio, sources_confirmed: confirmed,
    source_comments: Object.fromEntries(project.sources
      .filter(file => comments[file.position] !== undefined)
      .map(file => [file.position, comments[file.position]])),
  };
  const removeSource = async (position: number) => {
    const result = await command('delete_source', {source_position: position});
    if (!result.project) return;
    setComments(current => {
      const next = {...current};
      delete next[position];
      return next;
    });
    setConfirmedList(confirmed ? result.project.sources.map(file => file.position).join(',') : null);
  };
  const removeButton = (file: Project['sources'][number]) => (
    <button type="button" className="text-button danger remove-source"
      aria-label={`Удалить файл ${file.position} — ${file.name}`}
      onClick={() => void removeSource(file.position)}>Удалить</button>
  );
  return <section className="card form-card"><span className="eyebrow">ШАГ 1 / ИСХОДНЫЕ МАТЕРИАЛЫ</span><h2>О чём расскажем?</h2><p>Опишите идею своими словами. Можно без готового плана — разберёмся вместе.</p>
    <label>Описание презентации <span className="required">*</span><textarea value={description} maxLength={20000} onChange={e=>setDescription(e.target.value)} placeholder="Например: презентация новой услуги для руководителей малого бизнеса. Нужно объяснить пользу и пригласить на консультацию…" rows={5}/></label>
    <label>Формат слайдов<select value={ratio} onChange={e=>setRatio(e.target.value)}><option value="16:9">16:9 — широкоформатный</option><option value="4:3">4:3 — классический</option></select></label>
    <label className="upload-box">＋ Добавить исходные файлы<small>PDF, Word, Excel, PowerPoint, TXT, CSV и изображения<br/>До 10 файлов, каждый до 20 МБ</small><input type="file" multiple accept=".pdf,.doc,.docx,.ppt,.pptx,.xlsx,.txt,.csv,.jpg,.jpeg,.png,.webp" onChange={e=>{void upload('source',e.currentTarget.files);e.currentTarget.value='';}}/></label>
    {!!project.sources.length && (confirmed ? (
      <table className="source-comments" aria-label="Комментарии к исходным файлам">
        <caption>Укажите, что использовать из каждого файла. Комментарии необязательны.</caption>
        <colgroup><col className="source-name-column"/><col/></colgroup>
        <thead><tr><th scope="col">Файл</th><th scope="col">Комментарий</th></tr></thead>
        <tbody>{project.sources.map(file => (
          <tr key={file.position}>
            <th scope="row">{file.name}{removeButton(file)}</th>
            <td><textarea aria-label={`Комментарий к файлу ${file.position} — ${file.name}`}
              value={comments[file.position] ?? file.comment} rows={2} maxLength={2000}
              placeholder="Например, используйте данные из таблицы 2"
              onChange={e => setComments({...comments, [file.position]: e.target.value})}/></td>
          </tr>
        ))}</tbody>
      </table>
    ) : (
      <div className="source-confirmation">
        <ul className="file-list">{project.sources.map(file => <li key={file.position}><span aria-hidden="true">▤</span><span className="source-file-name">{file.name}</span>{removeButton(file)}</li>)}</ul>
        <button type="button" className="secondary" onClick={() => setConfirmedList(sourceList)}>Готово, все файлы добавлены</button>
      </div>
    ))}
    <p className="hint">Ссылки на документы пока не читаются. Прикрепите сами файлы.</p>
    <div className="form-actions"><button className="secondary" disabled={!description.trim()} onClick={()=>void command('save_sources',values)}>Сохранить черновик</button><button className="primary" disabled={analyzeDisabled} onClick={()=>void command('analyze',values)}>Изучить материалы →</button></div>
  </section>;
}
const usageKey = 'audience.usage';
const otherUsage = 'другое';

function surveyChoices(maxSlides: number): Record<string, {label?: string; options: string[]}> {
  return {
    [usageKey]: {label: 'Как будет использоваться презентация?', options: ['выступление', 'рассылка', 'все вместе', otherUsage]},
    'audience.viewers': {options: ['1', 'до 5', 'до 20', 'до 100', 'больше 100', 'не знаю']},
    'audience.volume': {label: 'Сколько слайдов?', options: Array.from({length: maxSlides}, (_, i) => String(i + 1))},
    'source.task': {label: 'Что нужно сделать?', options: ['Разработать презентацию с нуля', 'Переработать существующую презентацию']},
    'source.research': {options: ['да', 'нет']},
  };
}

function normalizeSurveyChoice(key: string, answer: string, options: string[]): string {
  const normalize = (value: string) => value.trim().toLowerCase().replaceAll('ё', 'е').replace(/[.!?]+$/, '');
  const value = normalize(answer);
  const exact = options.find(option => normalize(option) === value);
  if (exact) return exact;
  if (!value || value === 'не указано') return '';

  if (key === usageKey) {
    const speaking = /выступлен/.test(value);
    const mailing = /рассылк/.test(value);
    if (speaking && mailing && /(?:выступлен\S*\s+и\s+рассылк|рассылк\S*\s+и\s+выступлен)/.test(value)) return 'все вместе';
    if (/^(?:для )?выступлен(?:ие|ия)$/.test(value)) return 'выступление';
    if (/^(?:для )?рассылк[аиу]$/.test(value)) return 'рассылка';
    return otherUsage;
  }
  if (key === 'audience.viewers') {
    if (/^(?:неизвестно|пока не знаю)$/.test(value)) return 'не знаю';
    const match = value.match(/^(\d+)\s*(?:человек[аи]?|зрител(?:ь|я|ей))?$/);
    if (match && Number(match[1]) >= 1) {
      const count = Number(match[1]);
      if (count === 1) return '1';
      return [5, 20, 100].map(limit => ({limit, label: `до ${limit}`}))
        .find(option => count <= option.limit)?.label ?? 'больше 100';
    }
  }
  if (key === 'audience.volume') {
    const count = value.match(/^(\d+)\s+слайд(?:а|ов)?$/)?.[1];
    if (count && options.includes(count)) return count;
  }
  if (key === 'source.task') {
    if (/^(?:создать |разработать )?(?:презентацию )?с нуля$/.test(value)) return options[0];
    if (/^(?:переработать|оптимизировать)(?: (?:уже )?существующую)?(?: презентацию)?$/.test(value)) return options[1];
  }
  if (key === 'source.research') {
    if (/^(?:не нужен|не нужно|не требуется|нет, не (?:нужен|нужно|требуется))$/.test(value)) return 'нет';
    if (/^(?:нужен|нужно|требуется|да, (?:нужен|нужно|требуется))$/.test(value)) return 'да';
  }
  return '';
}

function Survey({project,command,maxSlides}: Props & {maxSlides: number}) {
  const choices = surveyChoices(maxSlides);
  const [answers, setAnswers] = useState<Record<string, string>>(() => Object.fromEntries(project.questions.map(question => {
    const raw = project.answers[question.key] || '';
    const choice = choices[question.key];
    return [question.key, choice ? normalizeSurveyChoice(question.key, raw, choice.options) : raw === 'Не указано' ? '' : raw];
  })));
  const [other, setOther] = useState(() => {
    const raw = project.answers[usageKey]?.trim() || '';
    return answers[usageKey] === otherUsage && raw.toLowerCase() !== otherUsage ? raw : '';
  });
  const needsOther = answers[usageKey] === otherUsage;
  const confirm = () => void command('confirm_survey', {
    answers: needsOther ? {...answers, [usageKey]: other.trim()} : answers,
  });
  return <section className="card form-card">
    <span className="eyebrow">ШАГ 2 / УТОЧНЕНИЯ</span><h2>Проверим, что всё поняли</h2>
    <p>Мы нашли часть ответов в ваших материалах. Дополните или исправьте их. Поля можно оставить пустыми.</p>
    <div className="survey-grid">{project.questions.map(question => {
      const choice = choices[question.key];
      const value = answers[question.key] || '';
      const raw = project.answers[question.key]?.trim();
      const change = (text: string) => setAnswers(current => ({...current, [question.key]: text}));
      return <div key={question.key}>
        <label>{choice?.label ?? question.text}{choice ?
          <select value={value} onChange={e => change(e.target.value)}>
            <option value="">Не выбрано</option>
            {choice.options.map(option => <option key={option}>{option}</option>)}
          </select> :
          <textarea value={value} onChange={e => change(e.target.value)} maxLength={10000} rows={3}/>}
        </label>
        {choice && !value && raw && raw.toLowerCase() !== 'не указано' && <p className="hint survey-previous-answer">Ранее найденный ответ: {raw}</p>}
        {question.key === usageKey && needsOther && <label>Как именно будет использоваться презентация?
          <textarea value={other} onChange={e => setOther(e.target.value)} maxLength={10000} rows={2}/>
        </label>}
      </div>;
    })}</div>
    <div className="form-actions"><button className="primary" disabled={needsOther && !other.trim()} onClick={confirm}>Подтвердить и подготовить план →</button></div>
  </section>;
}
function Plan({project,command,upload}: Props & Upload) {
  const [texts,setTexts] = useState<Record<number,string>>({});
  const [captions,setCaptions] = useState<Record<number,string>>({});
  const [saveErrors,setSaveErrors] = useState<Record<number,string>>({});
  const dirtyNumbers = project.slides.filter(s => texts[s.number] !== undefined && texts[s.number] !== s.text).map(s => s.number);
  const dirty = dirtyNumbers.length > 0;
  const discard = (number: number) => {
    setTexts(current => { const next = {...current}; delete next[number]; return next; });
    setSaveErrors(current => ({...current,[number]:''}));
  };
  const save = async (number: number) => {
    setSaveErrors(current => ({...current,[number]:''}));
    const result = await command('edit_slide',{number,text:texts[number]});
    if (result.project) {
      // The server trims whitespace and optional headers. Compare against its
      // canonical text after saving, without clearing drafts of other slides.
      discard(number);
    } else if (result.error) {
      setSaveErrors(current => ({...current,[number]:result.error!}));
    }
  };
  return <section className="plan-editor"><div className="card form-card"><span className="eyebrow">ШАГ 3 / СТРУКТУРА</span><h2>Сначала — правильная история</h2><p>Проверьте содержание каждого слайда. Измените текст и добавьте изображения, которые нужно использовать.</p>{project.notes.map((n,i)=><p className="note" key={i}>{n}</p>)}</div>
    {project.slides.map(s=><article className="card slide-editor" key={s.number}><div className="slide-number">{String(s.number).padStart(2,'0')}<small>СЛАЙД</small></div><div className="slide-body"><textarea aria-label={`Содержание слайда ${s.number}`} aria-invalid={!!saveErrors[s.number]} aria-describedby={saveErrors[s.number]?`slide-error-${s.number}`:undefined} rows={7} maxLength={20000} value={texts[s.number]??s.text} onChange={e=>{setTexts({...texts,[s.number]:e.target.value});setSaveErrors(current=>({...current,[s.number]:''}));}}/>{s.missing.map((n,i)=><p className="note" key={i}>Не хватает данных: {n}</p>)}
      {texts[s.number]!==undefined && texts[s.number]!==s.text && <div className="form-actions"><button className="secondary" onClick={()=>void save(s.number)}>Сохранить слайд</button><button className="text-button" onClick={()=>discard(s.number)}>Отменить правки</button></div>}
      {saveErrors[s.number] && <div className="alert error" id={`slide-error-${s.number}`} role="alert">{saveErrors[s.number]}</div>}
      {s.image && <div className="attached-image"><img src={s.image} alt={`Ваше изображение для слайда ${s.number}`}/><span>{s.caption||'Без подписи'}</span><button className="text-button danger" onClick={()=>void command('delete_image',{number:s.number})}>Убрать</button></div>}
      <div className="slide-photo-form"><label>Подпись к новому изображению<input maxLength={100} value={captions[s.number]||''} onChange={e=>setCaptions({...captions,[s.number]:e.target.value})} placeholder="Необязательно, до 100 символов"/></label><label className="upload-link">＋ {s.image?'Заменить':'Добавить'} изображение<input type="file" accept="image/jpeg,image/png,image/webp" onChange={e=>{void upload('slide',e.currentTarget.files,s.number,captions[s.number]);e.currentTarget.value='';}}/></label></div>
    </div></article>)}
    <div className="card form-card"><div className="form-actions">{dirty&&<span className="hint" role="status">Несохранённые слайды: {dirtyNumbers.join(', ')}. Нажмите «Сохранить слайд» под каждой изменённой карточкой.</span>}<button className="primary" disabled={dirty||!project.slides.length} onClick={()=>void command('accept_plan')}>План подходит, перейти к дизайну →</button></div></div>
  </section>;
}
type DownloadLink = { path: string; file_name: string; at: number };
// Inside Telegram a click must reach downloadFile/openLink right away, so signed links
// (valid 5 minutes on the server) are fetched in advance and refreshed after 4 minutes.
function Downloads({project}: {project: Project}) {
  const [links, setLinks] = useState<Record<string, DownloadLink>>({});
  const [error, setError] = useState('');
  const fetchLinks = useCallback(async () => {
    const entries = await Promise.all(project.files.map(async file => [file.kind,
      {...await api<{ path: string; file_name: string }>(`/projects/${project.id}/files/${file.kind}/link`, { method: 'POST' }), at: Date.now()}] as const));
    setLinks(Object.fromEntries(entries));
  }, [project.id, project.files.map(f => f.kind).join()]);
  useEffect(() => {
    if (!inTelegram() || !project.files.length) return;
    const load = () => void fetchLinks().catch(() => setError('Не удалось подготовить ссылки на файлы. Обновите страницу.'));
    load();
    const timer = window.setInterval(load, 4*60*1000);
    return () => window.clearInterval(timer);
  }, [fetchLinks, project.files.length]);
  return <><div className="downloads">{project.files.map(file => <a className="primary" key={file.kind} href={file.url} download onClick={e => {
    if (!inTelegram()) return;
    e.preventDefault();
    const link = links[file.kind];
    if (link && Date.now() - link.at < 4*60*1000) { setError(''); telegramDownload(window.location.origin + link.path, link.file_name); }
    else { setError('Готовим ссылку на файл, нажмите ещё раз через секунду.'); void fetchLinks().catch(() => setError('Не удалось подготовить ссылку на файл.')); }
  }}>↓ {file.label}</a>)}</div>
    {inTelegram() && <p className="hint">Файл откроется в окне загрузки Telegram. Его также можно получить в чате с ботом.</p>}
    {error && <div className="alert error" role="alert">{error}</div>}</>;
}
function Background({project,command}: Props) {
  const [comment,setComment] = useState('');
  return <section className="card form-card"><span className="eyebrow">ШАГ 4 / СОГЛАСОВАНИЕ ФОНА</span><h2>Основа для ваших слайдов</h2><p>Выберите фон. Содержание появится на нём на следующем этапе.</p><div className="background-grid">{project.backgrounds.map(bg=><div key={bg.choice}><img src={bg.url} alt={`Вариант фона ${bg.choice}`}/><button className="primary" onClick={()=>void command('accept_background',{choice:bg.choice})}>Принять {project.backgrounds.length>1?`вариант ${bg.choice}`:'фон'} →</button></div>)}</div>
    {project.can_change_background&&<details><summary>Изменить фон</summary><p>Можно подготовить один дополнительный вариант.</p><label>Что изменить?<textarea rows={3} maxLength={20000} value={comment} onChange={e=>setComment(e.target.value)} placeholder="Опишите, что понравилось и что нужно доработать"/></label><div className="form-actions"><button className="secondary" disabled={!comment.trim()} onClick={()=>void command('change_background',{change:'edit',text:comment})}>Доработать по комментарию</button><button className="secondary" onClick={()=>void command('change_background',{change:'redraw'})}>Нарисовать заново</button></div></details>}
  </section>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
