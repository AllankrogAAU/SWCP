using coreApi.Models;
using Microsoft.EntityFrameworkCore;

namespace coreApi.Data;

public sealed class CoreDbContext(DbContextOptions<CoreDbContext> options) : DbContext(options)
{
    public DbSet<User> Users => Set<User>();
    public DbSet<Assignment> Assignments => Set<Assignment>();
    public DbSet<Submission> Submissions => Set<Submission>();

    protected override void OnModelCreating(ModelBuilder modelBuilder)
    {
        modelBuilder.Entity<User>(entity =>
        {
            entity.HasKey(user => user.Id);
            entity.HasIndex(user => user.Username).IsUnique();
            entity.Property(user => user.Username).HasMaxLength(320).IsRequired();
            entity.Property(user => user.PasswordHash).IsRequired();
            entity.Property(user => user.Role).HasConversion<string>().HasMaxLength(16);
        });

        modelBuilder.Entity<Assignment>(entity =>
        {
            entity.HasKey(assignment => assignment.Id);
            entity.Property(assignment => assignment.Title).HasMaxLength(240).IsRequired();
            entity.Property(assignment => assignment.Description).IsRequired();
            entity.Property(assignment => assignment.TaskFocus).IsRequired();
            entity.Property(assignment => assignment.TestCasesJson).HasColumnType("jsonb").IsRequired();
        });

        modelBuilder.Entity<Submission>(entity =>
        {
            entity.HasKey(submission => submission.Id);
            entity.Property(submission => submission.Status).HasConversion<string>().HasMaxLength(32);
            entity.Property(submission => submission.SourceCode).IsRequired();
            entity.Property(submission => submission.Action).HasMaxLength(16).HasDefaultValue("submit").IsRequired();
            entity.Property(submission => submission.SandboxOutputJson).HasColumnType("jsonb");
            entity.Property(submission => submission.LlmFeedback).HasColumnType("text");
            entity.Property(submission => submission.TaskSolved);
            entity.Property(submission => submission.ErrorMessage).HasColumnType("text");
            entity.HasOne(submission => submission.User)
                .WithMany()
                .HasForeignKey(submission => submission.UserId)
                .OnDelete(DeleteBehavior.Restrict);
            entity.HasOne(submission => submission.Assignment)
                .WithMany()
                .HasForeignKey(submission => submission.AssignmentId)
                .OnDelete(DeleteBehavior.Restrict);
            entity.HasIndex(submission => new { submission.Status, submission.UpdatedAtUtc })
                .HasFilter("\"Status\" IN ('PENDING', 'SANDBOX_QUEUED', 'SANDBOX_PROCESSING', 'LLM_QUEUED', 'LLM_PROCESSING')");
            entity.HasIndex(submission => new { submission.UserId, submission.CreatedAtUtc })
                .IsDescending(false, true);
        });
    }
}