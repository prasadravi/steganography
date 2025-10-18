// lightweight particle background
(() => {
  const canvas = document.getElementById('bgParticles');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  let w = canvas.width = innerWidth;
  let h = canvas.height = innerHeight;
  const DPR = Math.max(1, window.devicePixelRatio || 1);
  canvas.width = w * DPR;
  canvas.height = h * DPR;
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  ctx.scale(DPR, DPR);

  // particle config
  const PARTICLE_COUNT = Math.round((w * h) / 90000); // scales with screen
  const particles = [];
  const colors = ['rgba(255,255,255,0.06)', 'rgba(255,255,255,0.04)', 'rgba(255,255,255,0.03)'];

  function rand(min, max) { return Math.random() * (max - min) + min; }

  function createParticle() {
    return {
      x: rand(0, w),
      y: rand(0, h),
      r: rand(0.8, 3.2),
      vx: rand(-0.15, 0.15),
      vy: rand(-0.05, 0.15),
      life: rand(8, 22),
      age: 0,
      color: colors[Math.floor(rand(0, colors.length))]
    };
  }

  for (let i = 0; i < PARTICLE_COUNT; i++) particles.push(createParticle());

  let last = performance.now();

  function step(now) {
    const dt = Math.min(50, now - last) / 1000;
    last = now;
    ctx.clearRect(0, 0, w, h);

    // draw soft vignette
    const grd = ctx.createRadialGradient(w * 0.5, h * 0.5, Math.min(w,h)*0.2, w * 0.5, h * 0.5, Math.max(w,h));
    grd.addColorStop(0, 'rgba(255,255,255,0.02)');
    grd.addColorStop(1, 'rgba(0,0,0,0.01)');
    ctx.fillStyle = grd;
    ctx.fillRect(0,0,w,h);

    // update particles
    for (let p of particles) {
      p.x += p.vx * (20 * dt);
      p.y += p.vy * (20 * dt);
      p.age += dt;
      const lifeRatio = Math.max(0, 1 - p.age / p.life);

      // wrap
      if (p.x < -20) p.x = w + 20;
      if (p.x > w + 20) p.x = -20;
      if (p.y < -20) p.y = h + 20;
      if (p.y > h + 20) p.y = -20;

      // draw glow
      const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r * 16);
      g.addColorStop(0, `rgba(255,255,255,${0.12 * lifeRatio})`);
      g.addColorStop(0.25, `rgba(255,255,255,${0.06 * lifeRatio})`);
      g.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.beginPath();
      ctx.fillStyle = g;
      ctx.arc(p.x, p.y, p.r * 8, 0, Math.PI * 2);
      ctx.fill();

      // faint core
      ctx.beginPath();
      ctx.fillStyle = `rgba(255,255,255,${0.06 * lifeRatio})`;
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();

      if (p.age >= p.life) {
        // reset
        Object.assign(p, createParticle());
      }
    }

    requestAnimationFrame(step);
  }

  window.addEventListener('resize', () => {
    w = canvas.width = innerWidth;
    h = canvas.height = innerHeight;
    canvas.width = w * DPR;
    canvas.height = h * DPR;
    canvas.style.width = w + 'px';
    canvas.style.height = h + 'px';
    ctx.scale(DPR, DPR);
  });

  requestAnimationFrame(step);
})();
