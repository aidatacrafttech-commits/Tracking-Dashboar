import { Link } from "react-router-dom";
import { EmptyState, PageHeader } from "../components/ui";

export function NotFoundPage() {
  return (
    <>
      <PageHeader title="Page not found" />
      <div className="card">
        <EmptyState title="There is nothing at this address." action={<Link to="/">Go to the overview</Link>} />
      </div>
    </>
  );
}
