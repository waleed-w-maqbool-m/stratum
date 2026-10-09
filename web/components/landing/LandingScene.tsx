"use client";

import { Canvas, useFrame, useThree } from "@react-three/fiber";
import type { MutableRefObject } from "react";
import { useEffect, useLayoutEffect, useMemo, useRef } from "react";
import * as THREE from "three";

import type { AtlasData } from "@/lib/atlas";

import { SCENE_LABELS } from "./labels";

/**
 * The landing page's Atlas: the same field as the Atlas page, directed by scroll.
 *
 * One number drives it, `progress` (0 at the top of the story, 1 at the end), and every
 * motion here is that number explaining something:
 *
 * - 0 → 0.38  the leftovers. At first every table looks alike, a swarm of amber. As the
 *   story moves on, the backups, archives and scratch copies lose their colour and sink
 *   below the grid, which is what excavation concluded about 72 of Pagila's 90 tables.
 * - 0.40 → 0.56  the joins. The schema declares none; the lines are the ones Stratum
 *   rebuilt from the data, drawn in one at a time.
 * - 0.70 → 1  the payments table. The camera closes on `fin_txn`, the table that holds
 *   payments, refunds and late fees together, and everything else steps back.
 *
 * The camera path is a handful of keyframes, eased between, with a slow sway so the
 * field reads as depth. Labels are plain DOM placed by projecting each table's position,
 * so the type stays crisp and selectable-by-screen-reader rather than drawn into the
 * canvas.
 */

export const STATIC_PROGRESS = 0.56;

const TARGET_ID = "public.fin_txn";

const AMBER = new THREE.Color("#ffb23f");
const BRIGHT = new THREE.Color("#ffd98a");
const DIM = new THREE.Color("#5a4420");
const DARK = new THREE.Color("#1c1e22");

const CLASS_COLOURS: Record<string, string> = {
  core: "#ffb23f",
  archive: "#8a6021",
  staging: "#6b7280",
  backup: "#4b5563",
  dead: "#2a2d33",
};

/** How far above their final depth the leftovers start, and how far in toward the
 * live cluster, so they begin as part of it. */
const LIFT = 12;
const PULL = 0.45;

type Key = { t: number; p: [number, number, number]; l: [number, number, number] };

const KEYS: Key[] = [
  { t: 0.0, p: [26, 13, 26], l: [0, -2, 0] },
  { t: 0.14, p: [26, 13, 26], l: [0, -2, 0] },
  { t: 0.38, p: [36, 0, 16], l: [0, -3, 0] },
  { t: 0.66, p: [13, 9, 13], l: [0, 1, 0] },
  { t: 1.0, p: [8, 4, 6], l: [-3.8, 0.8, -4.6] },
];

const clamp01 = (x: number) => Math.min(1, Math.max(0, x));
const range = (t: number, a: number, b: number) => clamp01((t - a) / (b - a));
const ease = (x: number) => x * x * (3 - 2 * x);

type Props = {
  data: AtlasData;
  progress: MutableRefObject<number>;
  labels: MutableRefObject<Record<string, HTMLElement | null>>;
  animated: boolean;
};

function Story({ data, progress, labels, animated }: Props) {
  const mesh = useRef<THREE.InstancedMesh>(null);
  const { camera, size } = useThree();
  const smooth = useRef(animated ? 0 : STATIC_PROGRESS);

  const scene = useMemo(() => {
    const index = new Map(data.nodes.map((node, i) => [node.id, i]));
    const isCore = data.nodes.map((node) => node.class === "core");
    const edges = data.edges.flatMap((edge) => {
      const a = index.get(edge.from);
      const b = index.get(edge.to);
      return a === undefined || b === undefined || !isCore[a] || !isCore[b] ? [] : [{ a, b, edge }];
    });
    return {
      index,
      isCore,
      edges,
      base: data.nodes.map((node) => new THREE.Vector3(...node.position)),
      classColour: data.nodes.map((node) => new THREE.Color(CLASS_COLOURS[node.class] ?? "#2a2d33")),
      radius: data.nodes.map((node) => 0.22 + Math.min(1.1, Math.log10(1 + node.columns) * 0.42)),
      target: index.get(TARGET_ID) ?? -1,
    };
  }, [data]);

  // The joins: one line segment per edge, drawn in order as the story reaches them.
  const lines = useMemo(() => {
    const positions = new Float32Array(scene.edges.length * 6);
    const colours = new Float32Array(scene.edges.length * 6);
    scene.edges.forEach(({ a, b, edge }, i) => {
      scene.base[a].toArray(positions, i * 6);
      scene.base[b].toArray(positions, i * 6 + 3);
      const shade = 0.65 + 0.35 * edge.confidence;
      const colour = new THREE.Color(edge.inferred ? "#9bc48a" : "#ffb23f").multiplyScalar(shade);
      colour.toArray(colours, i * 6);
      colour.toArray(colours, i * 6 + 3);
    });
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute("color", new THREE.BufferAttribute(colours, 3));
    const material = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0 });
    return new THREE.LineSegments(geometry, material);
  }, [scene]);

  useEffect(
    () => () => {
      lines.geometry.dispose();
      (lines.material as THREE.Material).dispose();
    },
    [lines],
  );

  // Allocated once: the frame loop below must not create garbage 60 times a second.
  const tmp = useMemo(
    () => ({
      matrix: new THREE.Matrix4(),
      position: new THREE.Vector3(),
      scale: new THREE.Vector3(),
      quaternion: new THREE.Quaternion(),
      colour: new THREE.Color(),
      eye: new THREE.Vector3(),
      look: new THREE.Vector3(),
      a: new THREE.Vector3(),
      b: new THREE.Vector3(),
      label: new THREE.Vector3(),
    }),
    [],
  );

  useLayoutEffect(() => {
    if (mesh.current) mesh.current.frustumCulled = false;
  }, [scene]);

  useFrame((state, delta) => {
    const instanced = mesh.current;
    if (!instanced) return;

    const goal = animated ? progress.current : STATIC_PROGRESS;
    smooth.current += (goal - smooth.current) * (1 - Math.exp(-5 * delta));
    const t = smooth.current;
    const time = animated ? state.clock.elapsedTime : 0;

    const sink = ease(range(t, 0.12, 0.38));
    const edgeProgress = range(t, 0.4, 0.56);
    const labelProgress = ease(range(t, 0.46, 0.58));
    const focus = ease(range(t, 0.7, 0.86));

    // --- the camera -------------------------------------------------------------
    let from = KEYS[0];
    let to = KEYS[KEYS.length - 1];
    for (let i = 0; i < KEYS.length - 1; i++) {
      if (t >= KEYS[i].t && t <= KEYS[i + 1].t) {
        from = KEYS[i];
        to = KEYS[i + 1];
        break;
      }
    }
    const u = from === to ? 0 : ease(range(t, from.t, to.t));
    tmp.a.set(...from.p).lerp(tmp.b.set(...to.p), u);
    tmp.look.set(...from.l).lerp(tmp.b.set(...to.l), u);
    const sway = Math.sin(time * 0.18) * 0.3 * (1 - 0.85 * focus);
    tmp.eye.copy(tmp.a).sub(tmp.look).applyAxisAngle(THREE.Object3D.DEFAULT_UP, sway).add(tmp.look);
    camera.position.copy(tmp.eye);
    camera.lookAt(tmp.look);

    // Text sits left of the field on a wide screen and below it on a narrow one, so the
    // image is shifted to the side that is free.
    const wide = size.width >= 1024;
    (camera as THREE.PerspectiveCamera).setViewOffset(
      size.width,
      size.height,
      wide ? -size.width * 0.22 : 0,
      wide ? 0 : size.height * 0.14,
      size.width,
      size.height,
    );

    // --- the tables -------------------------------------------------------------
    for (let i = 0; i < scene.base.length; i++) {
      tmp.position.copy(scene.base[i]);
      let scale = scene.radius[i];
      const live = scene.isCore[i];
      if (!live) {
        const k = 1 - sink;
        tmp.position.x *= 1 - PULL * k;
        tmp.position.z *= 1 - PULL * k;
        tmp.position.y += LIFT * k;
      }
      if (i === scene.target) scale *= 1 + focus * 0.7 + focus * 0.07 * Math.sin(time * 3);
      tmp.scale.setScalar(scale);
      tmp.matrix.compose(tmp.position, tmp.quaternion, tmp.scale);
      instanced.setMatrixAt(i, tmp.matrix);

      if (live) {
        tmp.colour.copy(AMBER);
        if (i === scene.target) tmp.colour.lerp(BRIGHT, focus);
        else tmp.colour.lerp(DIM, focus * 0.8);
      } else {
        tmp.colour.copy(AMBER).lerp(scene.classColour[i], sink).lerp(DARK, focus * 0.6);
      }
      instanced.setColorAt(i, tmp.colour);
    }
    instanced.instanceMatrix.needsUpdate = true;
    if (instanced.instanceColor) instanced.instanceColor.needsUpdate = true;

    // --- the joins --------------------------------------------------------------
    lines.geometry.setDrawRange(0, Math.floor(edgeProgress * scene.edges.length) * 2);
    (lines.material as THREE.LineBasicMaterial).opacity =
      Math.min(1, edgeProgress * 4) * 0.9 * (1 - 0.65 * focus);

    // --- the labels -------------------------------------------------------------
    for (const id of Object.keys(SCENE_LABELS)) {
      const element = labels.current[id];
      const i = scene.index.get(id);
      if (!element || i === undefined) continue;
      tmp.label.copy(scene.base[i]);
      tmp.label.y += 0.9 + scene.radius[i];
      tmp.label.project(camera);
      const x = (tmp.label.x * 0.5 + 0.5) * size.width;
      const y = (-tmp.label.y * 0.5 + 0.5) * size.height;
      element.style.transform = `translate3d(${x}px, ${y}px, 0) translate(-50%, -100%)`;
      const visible = tmp.label.z < 1 ? labelProgress * (id === TARGET_ID ? 1 : 1 - focus * 0.7) : 0;
      element.style.opacity = String(visible);
    }
  });

  return (
    <>
      <fog attach="fog" args={["#0b0c0e", 34, 90]} />
      <ambientLight intensity={0.55} />
      <directionalLight position={[12, 20, 8]} intensity={1.1} />
      <directionalLight position={[-14, -6, -10]} intensity={0.25} color="#ffb23f" />

      <instancedMesh ref={mesh} args={[undefined, undefined, data.nodes.length]}>
        <sphereGeometry args={[1, 18, 18]} />
        <meshStandardMaterial roughness={0.45} metalness={0.15} />
      </instancedMesh>
      <primitive object={lines} />
      <gridHelper args={[90, 45, "#1a1d22", "#141619"]} position={[0, -3, 0]} />
    </>
  );
}

export function LandingScene(props: Props & { running: boolean }) {
  const { running, ...rest } = props;
  return (
    <Canvas
      // `demand` costs nothing while idle: used for reduced motion, and whenever the
      // story has scrolled out of view.
      frameloop={rest.animated && running ? "always" : "demand"}
      dpr={[1, 1.5]}
      camera={{ position: [30, 15, 30], fov: 46, near: 0.1, far: 200 }}
      gl={{ antialias: true, alpha: true }}
      style={{ background: "transparent" }}
    >
      <Story {...rest} />
    </Canvas>
  );
}
