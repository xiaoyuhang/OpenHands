import { RepositorySelectionForm } from "./repo-selection-form";

interface RepoConnectorProps {
  onPathSelection: (path: string) => void;
}

export function RepoConnector({ onPathSelection }: RepoConnectorProps) {
  return (
    <section
      data-testid="repo-connector"
      className="w-full flex flex-col gap-6 rounded-[12px] p-[20px] border border-[#727987] bg-[#26282D] min-h-[263.5px] relative"
    >
      <RepositorySelectionForm
        onPathSelection={onPathSelection}
        isLoadingSettings={false}
      />
    </section>
  );
}
