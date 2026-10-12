import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { api } from "../api";
import { Badge, Empty, ErrorNote, Loading } from "../components/ui";
import type { Job } from "../types";

export default function JobsPage() {
  const q = useQuery({
    queryKey: ["jobs"], queryFn: api.jobs,
    refetchInterval: (query) => ((query.state.data as Job[] | undefined)?.some((j) => j.state === "running" || j.state === "queued") ? 1500 : 10_000),
  });
  const [open, setOpen] = useState<number | null>(null);
  const [cancelErr, setCancelErr] = useState<unknown>(null);
  const list = q.data ?? [];
  return (
    <>
      <h1>Jobs</h1>
      <Loading q={q} /><ErrorNote error={cancelErr} />
      {q.data && list.length === 0 && <Empty>No jobs yet.</Empty>}
      {list.length > 0 && (
        <div className="card scroll">
          <table className="t">
            <thead><tr><th>#</th><th>Type</th><th>State</th><th>Progress</th><th>Message</th><th /></tr></thead>
            <tbody>{list.map((j) => (
              <Fragment key={j.id}>
                <tr className="click" onClick={() => setOpen(open === j.id ? null : j.id)}>
                  <td>{j.id}</td><td>{j.type}</td><td><Badge value={j.state} label={j.state} /></td>
                  <td>{j.total > 0 ? `${j.progress}/${j.total}` : j.progress || ""}</td>
                  <td className="small">{j.message ?? ""}</td>
                  <td onClick={(e) => e.stopPropagation()}>{(j.state === "running" || j.state === "queued") && (
                    <button className="danger" onClick={() => api.cancelJob(j.id).then(() => q.refetch(), setCancelErr)}>Cancel</button>)}</td>
                </tr>
                {open === j.id && (
                  <tr><td colSpan={6}>
                    {j.error && <pre>{j.error}</pre>}
                    {j.result ? <pre>{JSON.stringify(j.result, null, 1)}</pre> : !j.error && <span className="muted">No result recorded.</span>}
                  </td></tr>
                )}
              </Fragment>
            ))}</tbody>
          </table>
        </div>
      )}
    </>
  );
}
