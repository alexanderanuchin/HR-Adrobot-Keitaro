import { useEffect, useState } from 'react'

type Section = 'overview' | 'create' | 'editor' | 'monitor' | 'settings'
type Health = 'checking' | 'online' | 'offline'

const navigation: { id: Section; label: string; subtitle: string }[] = [
  { id: 'overview', label: 'Обзор', subtitle: 'Состояние проекта' },
  { id: 'create', label: 'Создаватор', subtitle: 'Кампании Keitaro' },
  { id: 'editor', label: 'Редактор', subtitle: 'Потоки и офферы' },
  { id: 'monitor', label: 'Мониторинг', subtitle: 'Celery / Telegram' },
  { id: 'settings', label: 'Настройки', subtitle: 'Панель управления' },
]

const capability: Record<Section, { title: string; description: string; plans: string[] }> = {
  overview: {
    title: 'Инженерная панель Adrobot',
    description: 'Первый инфраструктурный этап. Проверяем основу приложения до подключения бизнес-логики.',
    plans: ['Django: учётные записи и административный API', 'FastAPI: изолированная интеграция Keitaro', 'PostgreSQL: единая БД, независимые владельцы таблиц'],
  },
  create: {
    title: 'Создаватор кампаний',
    description: 'Экран зарезервирован. Создание кампаний и двух потоков будет реализовано и проверено отдельным этапом.',
    plans: ['Название кампании, GEO и Offer ID', 'Домен, группа, источник', 'Country → Google; остальные → выбранный оффер'],
  },
  editor: {
    title: 'Редактор потоков',
    description: 'Экран зарезервирован. Пока нет действующих кнопок, способных изменять Keitaro.',
    plans: ['FETCH и черновики с сохранением состояния', 'ADD, REMOVE, BRING BACK и PIN', 'CANCEL, PUSH и безопасное разрешение конфликтов'],
  },
  monitor: {
    title: 'Автоматический мониторинг',
    description: 'Celery и Telegram имеют точки запуска. Настраиваемые проверки и доставка уведомлений будут добавлены позже.',
    plans: ['Сверка опубликованных данных Keitaro', 'Обнаружение расхождений', 'Уведомления Telegram-администраторам'],
  },
  settings: {
    title: 'Настройки системы',
    description: 'Административная модель и API управления настройками ещё не реализованы.',
    plans: ['Интервал проверок в минутах', 'Привязка Telegram администратора', 'Уведомления, права и журналы'],
  },
}

function App() {
  const [section, setSection] = useState<Section>('overview')
  const [health, setHealth] = useState<Health>('checking')

  useEffect(() => {
    const controller = new AbortController()
    fetch('/api/v1/health/', { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error('Health response not OK')
        setHealth('online')
      })
      .catch(() => {
        if (!controller.signal.aborted) setHealth('offline')
      })
    return () => controller.abort()
  }, [])

  const active = capability[section]
  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="Главная навигация">
        <div className="brand">
          <div className="brand__mark" aria-hidden="true">A</div>
          <div><strong>ADROBOT</strong><span>KEITARO WORKSPACE</span></div>
        </div>
        <div className="sidebar__group-label">РАБОЧЕЕ ПРОСТРАНСТВО</div>
        <nav className="navigation" aria-label="Разделы приложения">
          {navigation.map((item, index) => (
            <button
              className={item.id === section ? 'navigation__item navigation__item--active' : 'navigation__item'}
              key={item.id}
              type="button"
              onClick={() => setSection(item.id)}
              aria-current={section === item.id ? 'page' : undefined}
            >
              <span className="navigation__index">0{index + 1}</span>
              <span className="navigation__copy"><strong>{item.label}</strong><small>{item.subtitle}</small></span>
              <span className="navigation__arrow" aria-hidden="true">↗</span>
            </button>
          ))}
        </nav>
        <div className="sidebar__footer">A-01 — A-04 <span>АРХИТЕКТУРА СОГЛАСОВАНА</span></div>
      </aside>

      <main className="content">
        <header className="topbar">
          <span>PROJECT / ADROBOT <span className="topbar__divider">/</span> {navigation.find((item) => item.id === section)?.label.toUpperCase()}</span>
          <span className={`connection connection--${health}`}>
            <span className="connection__dot" aria-hidden="true" />
            {health === 'checking' ? 'Проверка Django API' : health === 'online' ? 'Django API доступен' : 'Django API недоступен'}
          </span>
        </header>

        <div className="page">
          <div className="eyebrow">STAGE 01 <span className="eyebrow__line" /> СТРУКТУРА ПРОЕКТА</div>
          <h1>{active.title}</h1>
          <p className="lead">{active.description}</p>
          <div className="banner"><span className="banner__symbol" aria-hidden="true">◎</span><div><strong>Режим каркаса</strong><p>Интеграция с Keitaro и изменение офферов ещё не подключены. Публикация невозможна — это безопасное исходное состояние.</p></div></div>

          <div className="section-heading"><span>ПЛАН ИНТЕГРАЦИИ</span><span>01 / 03</span></div>
          <div className="roadmap">
            {active.plans.map((plan, index) => (
              <div className="roadmap__card" key={plan}>
                <div className="roadmap__number">0{index + 1}</div>
                <p>{plan}</p>
                <span>ЗАПЛАНИРОВАНО</span>
              </div>
            ))}
          </div>
          <div className="footer-note"><span>GitHub: HR-Adrobot-Keitaro</span><span>Первый проверяемый коммит · Bootstrap</span></div>
        </div>
      </main>
    </div>
  )
}

export default App
