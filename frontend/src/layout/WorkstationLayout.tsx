import { useRef, useCallback } from 'react';
import { Layout, Model, Actions } from 'flexlayout-react';
import { useStore } from '../data/store';
import { componentFactory } from './ComponentFactory';
import { variantA, variantB, variantC, variantD } from './variants';
import type { LayoutVariant } from '../types';

const STORAGE_PREFIX = 'ib-layout-model-';
const TABS_PREFIX = 'ib-layout-tabs-';

// Set to true to log save/load activity to the browser console while
// debugging tab persistence. Safe to leave on — emits ~1 log per click.
const DEBUG_TAB_PERSIST = true;

const variantDefaults: Record<LayoutVariant, object> = {
  A: variantA, B: variantB, C: variantC, D: variantD,
};

// Identify a tabset by its sorted child tab names. Stable across reloads
// because tab `name` is set in variants.ts (vs auto-generated `id` which drifts).
// In variants.ts no two tabsets share the same set of child names, so this is unique.
//
// We use `getType() === 'tabset'` rather than `instanceof TabSetNode` because
// Vite/HMR can end up with multiple copies of the class across module
// boundaries, making instanceof return false for genuine tabset nodes —
// which silently filters everything out and produces an empty save map.
function tabsetKey(node: any): string {
  const names: string[] = [];
  for (const child of node.getChildren()) {
    if (child.getType() === 'tab') names.push(child.getName());
  }
  return names.slice().sort().join('|');
}

// Walk the model and collect {tabsetKey -> selectedTabName}.
function serializeTabSelection(model: Model): Record<string, string> {
  const out: Record<string, string> = {};
  model.visitNodes((node: any) => {
    if (node.getType() !== 'tabset') return;
    const children = node.getChildren();
    const idx = node.getSelected();
    if (idx >= 0 && idx < children.length) {
      const selected = children[idx];
      if (selected.getType() === 'tab') {
        out[tabsetKey(node)] = selected.getName();
      }
    }
  });
  return out;
}

// Walk the model and dispatch SELECT_TAB for each tabset that has a saved selection.
// Runs after Model.fromJson so it overrides whatever flexlayout's own restoration did.
function applyTabSelection(model: Model, map: Record<string, string>): void {
  model.visitNodes((node: any) => {
    if (node.getType() !== 'tabset') return;
    const wantedName = map[tabsetKey(node)];
    if (!wantedName) return;
    for (const child of node.getChildren()) {
      if (child.getType() === 'tab' && child.getName() === wantedName) {
        model.doAction(Actions.selectTab(child.getId()));
        return;
      }
    }
  });
}

// Tabs that were added to the default layout after a user may already
// have a persisted model. For each entry we inject the tab into the
// tabset that contains `anchor` if the tab isn't already anywhere in the
// model. Keeps a user's customized layout intact on upgrade rather than
// forcing a reset.
const MIGRATED_TABS: Array<{ component: string; name: string; anchor: string }> = [
  { component: 'errors', name: 'Errors', anchor: 'logs' },
];

function migrateLayoutJson(raw: any): any {
  if (!raw || typeof raw !== 'object') return raw;

  // Collect every tab component present anywhere in the model.
  const present = new Set<string>();
  const visit = (node: any) => {
    if (!node) return;
    if (node.type === 'tab' && node.component) present.add(node.component);
    if (Array.isArray(node.children)) node.children.forEach(visit);
  };
  visit(raw.layout);
  if (Array.isArray(raw.borders)) raw.borders.forEach(visit);

  for (const mig of MIGRATED_TABS) {
    if (present.has(mig.component)) continue;

    // Find the parent tabset (or border) whose direct children contain the
    // anchor tab, and append the new tab there.
    const injectInto = (node: any): boolean => {
      if (!node) return false;
      const isTabContainer =
        node.type === 'tabset' || node.type === 'border';
      if (isTabContainer && Array.isArray(node.children)) {
        const idx = node.children.findIndex(
          (c: any) => c && c.type === 'tab' && c.component === mig.anchor,
        );
        if (idx >= 0) {
          node.children.splice(idx + 1, 0, {
            type: 'tab',
            name: mig.name,
            component: mig.component,
          });
          return true;
        }
      }
      if (Array.isArray(node.children)) {
        for (const child of node.children) {
          if (injectInto(child)) return true;
        }
      }
      return false;
    };
    let injected = injectInto(raw.layout);
    if (!injected && Array.isArray(raw.borders)) {
      for (const b of raw.borders) {
        if (injectInto(b)) { injected = true; break; }
      }
    }
    if (DEBUG_TAB_PERSIST) {
      console.log('[tabs] migration', mig.component, injected ? 'injected' : 'anchor-not-found');
    }
  }
  return raw;
}

function loadModel(variant: LayoutVariant): Model {
  let model: Model;
  try {
    const saved = localStorage.getItem(STORAGE_PREFIX + variant);
    if (saved) {
      const migrated = migrateLayoutJson(JSON.parse(saved));
      model = Model.fromJson(migrated);
      // Persist the migrated JSON so the injection runs exactly once per
      // added tab, even if the user never interacts with the new tab.
      try {
        localStorage.setItem(STORAGE_PREFIX + variant, JSON.stringify(migrated));
      } catch { /* quota — ignore */ }
    } else {
      model = Model.fromJson(variantDefaults[variant]);
    }
  } catch (e) {
    if (DEBUG_TAB_PERSIST) console.warn('[tabs] model load failed, using default', e);
    model = Model.fromJson(variantDefaults[variant]);
  }
  // Layer our own tab-selection restoration on top — flexlayout's own
  // `selected` round-tripping is unreliable (toJson strips defaults).
  try {
    const tabsRaw = localStorage.getItem(TABS_PREFIX + variant);
    if (tabsRaw) {
      const map = JSON.parse(tabsRaw);
      if (DEBUG_TAB_PERSIST) console.log('[tabs] loading saved selection', variant, map);
      applyTabSelection(model, map);
    } else if (DEBUG_TAB_PERSIST) {
      console.log('[tabs] no saved selection for variant', variant);
    }
  } catch (e) {
    if (DEBUG_TAB_PERSIST) console.warn('[tabs] selection restore failed', e);
  }
  return model;
}

const variantModels: Record<string, Model> = {};

function getModel(variant: LayoutVariant): Model {
  if (!variantModels[variant]) {
    variantModels[variant] = loadModel(variant);
  }
  return variantModels[variant];
}

export function WorkstationLayout() {
  const activeVariant = useStore((s) => s.activeVariant);
  const prevVariant = useRef(activeVariant);

  // Reset model when variant changes
  if (prevVariant.current !== activeVariant) {
    // Force reload from localStorage or default for the new variant
    variantModels[activeVariant] = loadModel(activeVariant);
    prevVariant.current = activeVariant;
  }

  const model = getModel(activeVariant);

  const onModelChange = useCallback((m: Model) => {
    try {
      localStorage.setItem(STORAGE_PREFIX + activeVariant, JSON.stringify(m.toJson()));
    } catch { /* quota exceeded — ignore */ }
    try {
      const map = serializeTabSelection(m);
      localStorage.setItem(TABS_PREFIX + activeVariant, JSON.stringify(map));
      if (DEBUG_TAB_PERSIST) console.log('[tabs] saved selection', activeVariant, map);
    } catch (e) {
      if (DEBUG_TAB_PERSIST) console.warn('[tabs] save failed', e);
    }
  }, [activeVariant]);

  return (
    <div className="flex-1 relative">
      <Layout
        model={model}
        factory={componentFactory}
        realtimeResize={true}
        onModelChange={onModelChange}
      />
    </div>
  );
}
