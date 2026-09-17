(() => {
  const mq = window.matchMedia('(max-width: 700px)');
  const sidebar = document.querySelector('.sidebar');
  const top = document.querySelector('.top');
  if (!sidebar || !top) return;

  const style = document.createElement('style');
  style.id = 'codexia-mobile-nav-style';
  style.textContent = `
    .mobile-nav-toggle,.mobile-nav-overlay{display:none}
    @media (max-width:700px){
      .top{padding-left:64px!important;min-height:70px;height:auto}
      .mobile-nav-toggle{display:grid;place-items:center;position:absolute;left:12px;top:14px;width:42px;height:42px;border:0;border-radius:12px;background:#353090;color:#fff;font-size:23px;font-weight:800;z-index:1003;box-shadow:0 5px 16px rgba(25,28,70,.2);cursor:pointer}
      .sidebar{display:none!important}
      body.mobile-nav-open{overflow:hidden}
      body.mobile-nav-open .sidebar{display:block!important;position:fixed!important;inset:0 auto 0 0;width:min(84vw,320px);height:100dvh!important;z-index:1002;padding:20px 14px 28px!important;overflow-y:auto;box-shadow:12px 0 30px rgba(0,0,0,.28)}
      body.mobile-nav-open .sidebar .brand{justify-content:flex-start!important}
      body.mobile-nav-open .sidebar .brand span,body.mobile-nav-open .sidebar .nav span{display:inline!important}
      body.mobile-nav-open .sidebar .nav button,body.mobile-nav-open .sidebar .nav a{justify-content:flex-start!important;min-height:46px;font-size:15px}
      .mobile-nav-overlay{position:fixed;inset:0;background:rgba(15,20,45,.45);z-index:1001}
      body.mobile-nav-open .mobile-nav-overlay{display:block}
    }
  `;
  document.head.appendChild(style);

  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'mobile-nav-toggle';
  toggle.setAttribute('aria-label', 'Abrir menu');
  toggle.setAttribute('aria-expanded', 'false');
  toggle.innerHTML = '☰';
  top.prepend(toggle);

  const overlay = document.createElement('div');
  overlay.className = 'mobile-nav-overlay';
  overlay.setAttribute('aria-hidden', 'true');
  document.body.appendChild(overlay);

  function setOpen(open) {
    document.body.classList.toggle('mobile-nav-open', open && mq.matches);
    toggle.setAttribute('aria-expanded', open && mq.matches ? 'true' : 'false');
    toggle.setAttribute('aria-label', open && mq.matches ? 'Fechar menu' : 'Abrir menu');
    toggle.innerHTML = open && mq.matches ? '×' : '☰';
  }

  toggle.addEventListener('click', () => setOpen(!document.body.classList.contains('mobile-nav-open')));
  overlay.addEventListener('click', () => setOpen(false));
  sidebar.addEventListener('click', (event) => {
    if (event.target.closest('button[data-page],a')) setOpen(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') setOpen(false);
  });
  mq.addEventListener?.('change', () => setOpen(false));
})();
