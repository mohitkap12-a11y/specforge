import { Link } from "react-router-dom";
import { FileDown, Gauge, GitCompareArrows, Library, MessagesSquare } from "lucide-react";
import { PageHeader } from "../components/ui";

/** What a user sees on a tool's pages until an admin releases it to everyone (Settings → Feature rollout). */
export default function ComingSoonPage({ tool = "coverage" }: { tool?: "coverage" | "chat" }) {
  if (tool === "chat") {
    return (
      <>
        <PageHeader title="Chatbot" description="Ask questions about your documents and get answers with sources." />
        <section className="card coming-soon" aria-labelledby="soon-title">
          <div className="ico"><MessagesSquare size={22} /></div>
          <span className="badge violet">Coming soon</span>
          <h2 id="soon-title">The Chatbot is on its way</h2>
          <p className="muted">It is being tried out internally and will appear here, and in the corner of every page, as soon as it is released. Nothing to set up on your side.</p>
          <ul className="muted small">
            <li>Answers from the knowledge base and the parsed specs, with sources</li>
            <li>Chat history and follow-up suggestions</li>
            <li>Code and JSON answers you can copy and download</li>
          </ul>
          <div className="row">
            <Link to="/knowledge"><button className="primary"><Library size={15} /> Knowledge base</button></Link>
            <Link to="/convert"><button><FileDown size={15} /> Convert to Markdown</button></Link>
          </div>
        </section>
      </>
    );
  }
  return (
    <>
      <PageHeader title="Coverage Tool" description="Measure which commands, fields and requirements of the specs your tests actually cover." />
      <section className="card coming-soon" aria-labelledby="soon-title">
        <div className="ico"><Gauge size={22} /></div>
        <span className="badge violet">Coming soon</span>
        <h2 id="soon-title">The Coverage Tool is on its way</h2>
        <p className="muted">It is being tried out internally and will appear here, with all its pages in the menu, as soon as it is released. Nothing to set up on your side.</p>
        <ul className="muted small">
          <li>Builds the list of everything that can be covered from the specs</li>
          <li>Derives one scenario per executed test from your command log</li>
          <li>Covered / partial / not covered with evidence; Excel, HTML, CSV and JSON reports</li>
        </ul>
        <div className="row">
          <Link to="/compare"><button className="primary"><GitCompareArrows size={15} /> Compare spec versions</button></Link>
          <Link to="/convert"><button><FileDown size={15} /> Convert to Markdown</button></Link>
        </div>
      </section>
    </>
  );
}
