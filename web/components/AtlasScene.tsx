"use client";

import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import type { MutableRefObject } from "react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";

import { ORIGIN_COLOR, originOf, type AtlasData, type AtlasNode } from "@/lib/atlas";

/**
 * The schema field in three dimensions (PLAN.md 5.3, 5.4).
 *
 * The arrangement carries the meaning, which is the only reason it is 3D at all:
 *
 * - **Height is size.** A node's Y position is the log of its row count, so the big
 *   tables stand up out of the field.
 * - **Depth is relevance.** Everything excavation classified as archive, staging,
 *   backup or dead sinks below the plane and dims. Seeing 117 of 134 tables sink is
 *   the single most informative thing about a legacy database.
 * - **Edge brightness is confidence.** A join read out of a working view is drawn solid;
 *   one inferred from an inclusion test is faint. You can see which parts of the graph
 *   are guesses.
 *
 * Performance budget (5.4): instanced meshes, labels only for the live tables and the
 * selection, `frameloop="demand"` so an idle scene costs nothing, DPR clamped to 1.5.
 *
 * Labels and the hover tooltip are ordinary DOM laid over the canvas and placed by
 * projecting each table's position every frame. They used to be drei `Html` elements,
 * each of which mounts its own React root inside the canvas; tearing those down while
 * the map was redrawn (as the schema page does on every edit) threw a DOM
 * `removeChild` error. Plain elements owned by this component have no such problem.
 */

const CLASS_COLOURS: Record<string, string> = {
  core: "#ffb23f", // phosphor: the live schema
  archive: "#8a6021", // phosphor, dimmed
  staging: "#6b7280",
  backup: "#4b5563",
  dead: "#2a2d33", // slate: barely there
};

function colourFor(node: AtlasNode): THREE.Color {
  return new THREE.Color(CLASS_COLOURS[node.class] ?? "#2a2d33");
}

/** Node radius from column count: a 53-column god table should look like one. */
function radiusFor(node: AtlasNode): number {
  return 0.22 + Math.min(1.1, Math.log10(1 + node.columns) * 0.42);
}

function Nodes({
  nodes,
  selected,
  onSelect,
  onHover,
}: {
  nodes: AtlasNode[];
  selected: string | null;
  onSelect: (id: string | null) => void;
  onHover: (id: string | null) => void;
}) {
  const meshRef = useRef<THREE.InstancedMesh>(null);

  // One instanced mesh for every node. The budget allows 5k; a legacy database of 134
  // is nothing, but the same code has to hold up on one with four hundred.
  const { positions, colours, scales } = useMemo(() => {
    const positions = nodes.map((node) => new THREE.Vector3(...node.position));
    const colours = nodes.map(colourFor);
    const scales = nodes.map(radiusFor);
    return { positions, colours, scales };
  }, [nodes]);

  // After mount, not during render: before the mesh exists there is nothing to write
  // to, and every instance would sit at the origin as one uncoloured sphere.
  useLayoutEffect(() => {
    const mesh = meshRef.current;
    if (!mesh) return;
    const matrix = new THREE.Matrix4();
    nodes.forEach((node, index) => {
      const emphasis = selected === null || selected === node.id ? 1 : 0.75;
      matrix.makeScale(
        scales[index] * emphasis,
        scales[index] * emphasis,
        scales[index] * emphasis,
      );
      matrix.setPosition(positions[index]);
      mesh.setMatrixAt(index, matrix);
      mesh.setColorAt(index, colours[index]);
    });
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    mesh.computeBoundingSphere();
  }, [nodes, positions, colours, scales, selected]);

  return (
    <instancedMesh
      ref={meshRef}
      args={[undefined, undefined, nodes.length]}
      onPointerMove={(event) => {
        event.stopPropagation();
        const index = event.instanceId;
        onHover(index === undefined ? null : (nodes[index]?.id ?? null));
      }}
      onPointerOut={() => onHover(null)}
      onClick={(event) => {
        event.stopPropagation();
        const index = event.instanceId;
        if (index === undefined) return;
        onSelect(nodes[index].id === selected ? null : nodes[index].id);
      }}
    >
      <sphereGeometry args={[1, 16, 16]} />
      <meshStandardMaterial roughness={0.45} metalness={0.15} />
    </instancedMesh>
  );
}

function Edges({
  data,
  selected,
  showGhosts,
}: {
  data: AtlasData;
  selected: string | null;
  showGhosts: boolean;
}) {
  const byId = useMemo(
    () => new Map(data.nodes.map((node) => [node.id, node])),
    [data.nodes],
  );

  // Colour is where the join came from; brightness is confidence. A declared key is
  // solid amber, one inferred statistically is a fainter green.
  const lines = useMemo(
    () =>
      data.edges.flatMap((edge, index) => {
        const from = byId.get(edge.from);
        const to = byId.get(edge.to);
        if (!from || !to) return [];
        const involved = selected === null || selected === edge.from || selected === edge.to;
        const geometry = new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(...from.position),
          new THREE.Vector3(...to.position),
        ]);
        const material = new THREE.LineBasicMaterial({
          color: new THREE.Color(ORIGIN_COLOR[originOf(edge)]),
          transparent: true,
          opacity: involved ? 0.25 + edge.confidence * 0.65 : 0.06,
        });
        return [{ key: `${edge.from}-${edge.to}-${index}`, line: new THREE.Line(geometry, material) }];
      }),
    [data.edges, byId, selected],
  );
  // Links Stratum thinks are missing: dashed and faint, so they read as a suggestion
  // beside the solid lines that are believed.
  const ghosts = useMemo(
    () =>
      !showGhosts
        ? []
        : (data.ghost_edges ?? []).flatMap((edge, index) => {
            const from = byId.get(edge.from);
            const to = byId.get(edge.to);
            if (!from || !to) return [];
            const geometry = new THREE.BufferGeometry().setFromPoints([
              new THREE.Vector3(...from.position),
              new THREE.Vector3(...to.position),
            ]);
            const material = new THREE.LineDashedMaterial({
              color: 0xc9ced6,
              dashSize: 0.5,
              gapSize: 0.4,
              transparent: true,
              opacity: selected === null || selected === edge.from || selected === edge.to ? 0.55 : 0.12,
            });
            const line = new THREE.Line(geometry, material);
            line.computeLineDistances();
            return [{ key: `ghost-${edge.from}-${edge.to}-${index}`, line }];
          }),
    [data.ghost_edges, byId, selected, showGhosts],
  );
  useLayoutEffect(
    () => () =>
      ghosts.forEach(({ line }) => {
        line.geometry.dispose();
        (line.material as THREE.Material).dispose();
      }),
    [ghosts],
  );
  useLayoutEffect(
    () => () =>
      lines.forEach(({ line }) => {
        line.geometry.dispose();
        (line.material as THREE.Material).dispose();
      }),
    [lines],
  );

  return (
    <>
      {lines.map(({ key, line }) => (
        <primitive key={key} object={line} />
      ))}
      {ghosts.map(({ key, line }) => (
        <primitive key={key} object={line} />
      ))}
    </>
  );
}

/** A slow drift, so the field reads as depth rather than as a flat picture. */
function Drift({ enabled }: { enabled: boolean }) {
  const { camera, invalidate } = useThree();
  useFrame((state) => {
    if (!enabled) return;
    const t = 0.785 + state.clock.elapsedTime * 0.05;
    camera.position.x = Math.cos(t) * 26;
    camera.position.z = Math.sin(t) * 26;
    camera.lookAt(0, 0, 0);
    invalidate();
  });
  return null;
}

type Elements = MutableRefObject<Record<string, HTMLElement | null>>;

/**
 * Places the DOM labels and the hover tooltip over their tables. It renders nothing: its
 * only job is to write each element's position from the camera, every frame the scene
 * draws, and to ask for one more frame when the set of labels changes.
 */
function Projector({
  nodes,
  labelled,
  hovered,
  labels,
  tooltip,
}: {
  nodes: AtlasNode[];
  labelled: string[];
  hovered: string | null;
  labels: Elements;
  tooltip: MutableRefObject<HTMLElement | null>;
}) {
  const { camera, size, invalidate } = useThree();
  const byId = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const point = useMemo(() => new THREE.Vector3(), []);

  useEffect(() => {
    invalidate();
  }, [labelled, hovered, invalidate]);

  useFrame(() => {
    const place = (element: HTMLElement | null | undefined, id: string, above: boolean) => {
      const node = byId.get(id);
      if (!element || !node) return;
      point.set(...node.position).project(camera);
      if (point.z > 1) {
        element.style.opacity = "0";
        return;
      }
      const x = (point.x * 0.5 + 0.5) * size.width;
      const y = (-point.y * 0.5 + 0.5) * size.height;
      element.style.transform = above
        ? `translate3d(${x}px, ${y - 18}px, 0) translate(-50%, -100%)`
        : `translate3d(${x}px, ${y + 22}px, 0) translate(-50%, 0)`;
      element.style.opacity = "1";
    };
    for (const id of labelled) place(labels.current[id], id, true);
    if (hovered) place(tooltip.current, hovered, false);
  });
  return null;
}

export function AtlasScene({
  data,
  selected,
  onSelect,
  reducedMotion,
  showGhosts = false,
}: {
  data: AtlasData;
  selected: string | null;
  onSelect: (id: string | null) => void;
  reducedMotion: boolean;
  showGhosts?: boolean;
}) {
  const [held, setHeld] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null);
  const labels = useRef<Record<string, HTMLElement | null>>({});
  const tooltip = useRef<HTMLElement | null>(null);

  // Labels for the live tables and the selection only (5.4): the excluded ones are the
  // point of sinking them, and naming all of them would bury the ones that matter.
  const labelled = useMemo(
    () =>
      data.nodes
        .filter((node) => node.class === "core" || node.id === selected)
        .slice(0, 40)
        .map((node) => node.id),
    [data.nodes, selected],
  );
  const nodeById = useMemo(() => new Map(data.nodes.map((node) => [node.id, node])), [data.nodes]);
  const hoveredNode = hovered ? nodeById.get(hovered) : undefined;

  return (
    <div className="relative h-full w-full" style={{ background: "#0b0c0e" }}>
      <Canvas
        // `demand` means an idle scene costs no frames at all, which is what keeps this
        // viable on an integrated GPU.
        frameloop={reducedMotion || held ? "demand" : "always"}
        onPointerMissed={() => onSelect(null)}
        dpr={[1, 1.5]}
        camera={{ position: [22, 12, 22], fov: 48 }}
        style={{ background: "#0b0c0e" }}
      >
        <ambientLight intensity={0.55} />
        <directionalLight position={[12, 20, 8]} intensity={1.1} />
        <directionalLight position={[-14, -6, -10]} intensity={0.25} color="#ffb23f" />

        <Nodes nodes={data.nodes} selected={selected} onSelect={onSelect} onHover={setHovered} />
        <Edges data={data} selected={selected} showGhosts={showGhosts} />

        {/* The plane the live schema sits on; everything excluded is below it. */}
        <gridHelper args={[80, 40, "#1a1d22", "#141619"]} position={[0, -3, 0]} />

        <Projector
          nodes={data.nodes}
          labelled={labelled}
          hovered={hovered}
          labels={labels}
          tooltip={tooltip}
        />
        <Drift enabled={!reducedMotion && !held} />
        <OrbitControls
          enableDamping
          dampingFactor={0.08}
          minDistance={6}
          maxDistance={70}
          makeDefault
          onStart={() => setHeld(true)}
        />
      </Canvas>

      <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
        {labelled.map((id) => {
          const node = nodeById.get(id);
          if (!node) return null;
          return (
            <span
              key={id}
              ref={(element) => {
                labels.current[id] = element;
              }}
              className="absolute left-0 top-0 whitespace-nowrap text-[12px] opacity-0"
              style={{
                color: id === selected ? "#ffb23f" : "#e9e4d8",
                textShadow: "0 0 6px #0b0c0e, 0 0 3px #0b0c0e",
              }}
            >
              {node.entity ?? node.id}
            </span>
          );
        })}
        {hoveredNode && (
          <span
            ref={(element) => {
              tooltip.current = element;
            }}
            className="absolute left-0 top-0 whitespace-nowrap border px-2 py-1 text-[12px] opacity-0"
            style={{ background: "#0b0c0e", borderColor: "#2a2d33", color: "#e9e4d8" }}
          >
            {hoveredNode.id},{" "}
            {hoveredNode.rows > 0
              ? `${hoveredNode.rows.toLocaleString()} rows`
              : `${hoveredNode.columns} columns`}
          </span>
        )}
      </div>
    </div>
  );
}
