// 自研 SVG 力导向布局内核（S7b）——纯函数、无 DOM，UI 层只管把位置画出来。
//
// 四件力学（力模拟的经典配方）：
// - 库仑斥力：所有节点对互斥，防堆叠（O(n²)；个人图谱几十到几百节点，
//   每帧几千对，CPU 足够）
// - 胡克弹簧：有边相连的节点拉向自然长度，关系近的聚在一起
// - 中心引力：拉向画布中心，防整体漂移出视野
// - 阻尼：速度逐帧衰减（<1），布局收敛停摆

const REPULSION = 1400;   // 库仑斥力系数
const SPRING = 0.03;      // 弹簧劲度
const REST_LENGTH = 90;   // 弹簧自然长度（像素）
const GRAVITY = 0.0015;   // 中心引力
const DAMPING = 0.85;     // 速度衰减（<1 收敛）

export function initLayout(n, width, height) {
  // 初始随机散点（中心附近高斯式铺开），零速度
  const span = Math.min(width, height) * 0.6;
  return Array.from({ length: n }, () => ({
    x: width / 2 + (Math.random() - 0.5) * span,
    y: height / 2 + (Math.random() - 0.5) * span,
    vx: 0,
    vy: 0,
  }));
}

export function tick(positions, edges, width, height) {
  const n = positions.length;
  // 1) 斥力：全对累加到速度（重叠时给一个确定方向的补偿，防 d=0 除零）
  for (let i = 0; i < n; i++) {
    const a = positions[i];
    for (let j = i + 1; j < n; j++) {
      const b = positions[j];
      let dx = b.x - a.x;
      let dy = b.y - a.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = 1; dy = 0; d2 = 1; }
      const d = Math.sqrt(d2);
      const rep = REPULSION / d2;
      const fx = (dx / d) * rep;
      const fy = (dy / d) * rep;
      a.vx -= fx; a.vy -= fy;
      b.vx += fx; b.vy += fy;
    }
  }
  // 2) 弹簧：边两端相向/相背，拉向自然长度
  for (const [s, t] of edges) {
    const a = positions[s];
    const b = positions[t];
    let dx = b.x - a.x;
    let dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = SPRING * (d - REST_LENGTH);
    const fx = (dx / d) * f;
    const fy = (dy / d) * f;
    a.vx += fx; a.vy += fy;
    b.vx -= fx; b.vy -= fy;
  }
  // 3) 中心引力 + 阻尼 + 积分
  for (let i = 0; i < n; i++) {
    const p = positions[i];
    p.vx += (width / 2 - p.x) * GRAVITY;
    p.vy += (height / 2 - p.y) * GRAVITY;
    p.vx *= DAMPING;
    p.vy *= DAMPING;
    p.x += p.vx;
    p.y += p.vy;
  }
}

export function energy(positions) {
  // 总动能：收敛判据（低于阈值即停摆，省 CPU）
  return positions.reduce((s, p) => s + p.vx * p.vx + p.vy * p.vy, 0);
}