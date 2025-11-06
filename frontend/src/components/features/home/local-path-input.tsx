import React from "react";
import { useTranslation } from "react-i18next";
import { I18nKey } from "#/i18n/declaration";
import { cn } from "#/utils/utils";
import FolderIcon from "#/icons/folder.svg?react";

export interface LocalPathInputProps {
  value: string;
  onChange: (path: string) => void;
  placeholder?: string;
  className?: string;
  disabled?: boolean;
}

export function LocalPathInput({
  value,
  onChange,
  placeholder,
  className,
  disabled = false,
}: LocalPathInputProps) {
  const { t } = useTranslation();

  const handleInputChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    onChange(event.target.value);
  };

  return (
    <div className={cn("relative", className)}>
      <div className="relative">
        <div className="absolute left-2 top-1/2 transform -translate-y-1/2 z-10">
          <FolderIcon width={16} height={16} />
        </div>
        <input
          type="text"
          value={value}
          onChange={handleInputChange}
          disabled={disabled}
          placeholder={placeholder || t(I18nKey.HOME$LOCAL_PATH_PLACEHOLDER)}
          className={cn(
            "w-full px-3 py-2 border border-[#727987] rounded-sm shadow-none h-[42px] min-h-[42px] max-h-[42px]",
            "bg-[#454545] text-[#A3A3A3] placeholder:text-[#A3A3A3]",
            "focus:outline-none focus:ring-0 focus:border-[#727987]",
            "disabled:bg-[#363636] disabled:cursor-not-allowed disabled:opacity-60",
            "pl-7 pr-3 text-sm font-normal leading-5",
          )}
          data-testid="local-path-input"
        />
      </div>
    </div>
  );
}
