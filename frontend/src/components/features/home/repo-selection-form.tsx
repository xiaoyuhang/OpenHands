import React from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router";
import { useCreateConversation } from "#/hooks/mutation/use-create-conversation";
import { useIsCreatingConversation } from "#/hooks/use-is-creating-conversation";
import { BrandButton } from "../settings/brand-button";
import { I18nKey } from "#/i18n/declaration";
import FolderIcon from "#/icons/folder.svg?react";
import { LocalPathInput } from "./local-path-input";

interface RepositorySelectionFormProps {
  onPathSelection: (path: string) => void;
  isLoadingSettings?: boolean;
}

export function RepositorySelectionForm({
  onPathSelection,
  isLoadingSettings = false,
}: RepositorySelectionFormProps) {
  const navigate = useNavigate();

  const [selectedPath, setSelectedPath] = React.useState<string>("");

  const {
    mutate: createConversation,
    isPending,
    isSuccess,
  } = useCreateConversation();

  const isCreatingConversationElsewhere = useIsCreatingConversation();

  const { t } = useTranslation();

  // We check for isSuccess because the app might require time to render
  // into the new conversation screen after the conversation is created.
  const isCreatingConversation =
    isPending || isSuccess || isCreatingConversationElsewhere;

  const handlePathChange = (path: string) => {
    setSelectedPath(path);
    onPathSelection(path);
  };

  return (
    <div className="flex flex-col">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-[10px] pb-4">
          <FolderIcon width={24} height={24} />
          <span className="leading-5 font-bold text-base text-white">
            {t(I18nKey.HOME$SELECT_OR_INSERT_URL)}
          </span>
        </div>
      </div>

      <div className="flex flex-col gap-[10px] pb-4">
        <LocalPathInput
          value={selectedPath}
          onChange={handlePathChange}
          disabled={isLoadingSettings}
          className="max-w-auto"
        />
      </div>

      <BrandButton
        testId="repo-launch-button"
        variant="primary"
        type="button"
        isDisabled={
          !selectedPath.trim() || isCreatingConversation || isLoadingSettings
        }
        onClick={() => {
          createConversation(
            {
              localPath: selectedPath,
            },
            {
              onSuccess: (data) =>
                navigate(`/conversations/${data.conversation_id}`),
            },
          );
        }}
        className="w-full font-semibold"
      >
        {!isCreatingConversation && "Launch"}
        {isCreatingConversation && t("HOME$LOADING")}
      </BrandButton>
    </div>
  );
}
