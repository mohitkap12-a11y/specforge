import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { SET_KEY, useStoredState } from "../hooks";

/** Remembers the selected spec set across pages (per browser). */
export function useSelectedSet() {
  const sets = useQuery({ queryKey: ["sets"], queryFn: api.sets });
  const [id, setId] = useStoredState<number | null>(SET_KEY, null);
  const list = sets.data ?? [];
  const valid = id !== null && list.some((s) => s.id === id);
  useEffect(() => { if (!valid && list.length) setId(list[0].id); }, [valid, list, setId]);
  return { sets: list, setId: valid ? id : null, setSetId: setId, loading: sets.isLoading, error: sets.error };
}

export function SetPicker({ sel }: { sel: ReturnType<typeof useSelectedSet> }) {
  if (!sel.sets.length) return <span className="muted">No spec sets yet – create one on the Specs page.</span>;
  return (
    <label className="row">Spec set
      <select value={sel.setId ?? ""} onChange={(e) => sel.setSetId(Number(e.target.value))}>
        {sel.sets.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
      </select>
    </label>
  );
}
