'use client'

import * as React from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { cn } from '@/lib/utils'

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string
  error?: string
  icon?: React.ReactNode
}

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, label, error, icon, id, type, ...props }, ref) => {
    const inputId = id || label?.toLowerCase().replace(/\s+/g, '-')
    const [showPassword, setShowPassword] = React.useState(false)
    const isPassword = type === 'password'

    return (
      <div className="flex flex-col gap-1.5">
        {label && (
          <label
            htmlFor={inputId}
            className="text-sm font-medium text-text-secondary"
          >
            {label}
          </label>
        )}
        <div className="relative">
          {icon && (
            <div className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-text-tertiary">
              {icon}
            </div>
          )}
          <input
            id={inputId}
            ref={ref}
            type={isPassword && showPassword ? 'text' : type}
            className={cn(
              'flex h-10 w-full rounded-md border border-border bg-bg-secondary px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary',
              // outline-none applies before and after focus alike. As focus:outline-none it
              // only took effect on focus, so transition-all animated the outline from
              // the text colour to transparent: a white ring that flashed and faded.
              // The focus glow is the accent at 20%. Written out with color-mix: the
              // accent is a bare var(), and Tailwind's /20 modifier compiles to no
              // CSS for those, which left the default blue ring instead.
              'outline-none transition-all duration-150 focus:border-accent focus:ring-2 focus:ring-[color:color-mix(in_srgb,var(--accent)_20%,transparent)]',
              'disabled:cursor-not-allowed disabled:opacity-50',
              icon && 'pl-9',
              isPassword && 'pr-9',
              error && 'border-status-error focus:border-status-error focus:ring-[color:color-mix(in_srgb,var(--status-error)_20%,transparent)]',
              className,
            )}
            {...props}
          />
          {isPassword && (
            <button
              type="button"
              tabIndex={-1}
              onClick={() => setShowPassword((v) => !v)}
              className="absolute inset-y-0 right-2.5 flex items-center text-text-tertiary hover:text-text-secondary transition-colors"
            >
              {showPassword ? (
                <EyeOff className="h-4 w-4" />
              ) : (
                <Eye className="h-4 w-4" />
              )}
            </button>
          )}
        </div>
        {error && (
          <p className="text-xs text-status-error">{error}</p>
        )}
      </div>
    )
  },
)
Input.displayName = 'Input'

export { Input }
